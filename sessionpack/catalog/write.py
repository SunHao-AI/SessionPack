"""catalog 的写入：项目/主题/消息 upsert、哈希刷新、导入进度表。

FTS 索引与 messages 表必须成对写入，故 upsert_message 内的
message_fts INSERT 与 messages INSERT 不可拆散。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import NamedTuple

from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord


class WriteResult(NamedTuple):
    inserted: bool
    session_created: bool


def upsert_project(conn: sqlite3.Connection, project: ProjectRecord) -> None:
    conn.execute(
        "INSERT INTO projects(project_id, display_name, local_path_hint, source_kind)"
        " VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
        " display_name=excluded.display_name, local_path_hint=excluded.local_path_hint",
        (project.project_id, project.display_name,
         project.local_path_hint, project.source_kind),
    )


def upsert_topic(conn: sqlite3.Connection, rec: TopicRecord) -> WriteResult:
    existing = conn.execute(
        "SELECT updated_at FROM sessions WHERE session_id=?", (rec.session_id,)
    ).fetchone()
    created = existing is None
    title = rec.body[:60] or None
    if created:
        conn.execute(
            "INSERT INTO sessions(session_id, project_id, source_kind, title, summary,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (rec.session_id, rec.project_id, rec.source_kind, title, rec.body,
             rec.topic_time, rec.topic_time),
        )
        return WriteResult(True, True)
    if rec.topic_time >= (existing["updated_at"] or ""):
        conn.execute(
            "UPDATE sessions SET project_id=?, title=?, summary=?, updated_at=?"
            " WHERE session_id=?",
            (rec.project_id, title, rec.body, rec.topic_time, rec.session_id),
        )
    return WriteResult(True, False)


def upsert_message(conn: sqlite3.Connection, rec: MessageRecord) -> WriteResult:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO messages(session_id, message_id, seq, ts, intent,"
        " outcome, actions, learned, meta_json, body) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rec.session_id, rec.message_id, rec.seq, rec.ts, rec.intent, rec.outcome,
         "|".join(rec.actions), "|".join(rec.learned),
         json.dumps(rec.meta, ensure_ascii=False) if rec.meta else None, rec.body),
    )
    if cursor.rowcount == 0:
        return WriteResult(False, False)
    conn.execute(
        "INSERT INTO message_fts(session_id, message_id, text) VALUES(?,?,?)",
        (rec.session_id, rec.message_id, rec.body),
    )
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (rec.session_id,)
    ).fetchone()
    created = existing is None
    if created:
        conn.execute(
            "INSERT INTO sessions(session_id, project_id, source_kind, title,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (rec.session_id, rec.project_id, rec.source_kind,
             (rec.intent or rec.body)[:60] or None, rec.ts, rec.ts),
        )
    else:
        conn.execute(
            "UPDATE sessions SET project_id=?,"
            " updated_at=MAX(COALESCE(updated_at,''), COALESCE(?, ''))"
            " WHERE session_id=?",
            (rec.project_id, rec.ts, rec.session_id),
        )
    return WriteResult(True, created)


def refresh_session_hashes(conn: sqlite3.Connection, session_ids: set[str]) -> None:
    for session_id in sorted(session_ids):
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM messages WHERE session_id=?"
                " ORDER BY seq, message_id",
                (session_id,),
            )
        ]
        digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE sessions SET content_hash=? WHERE session_id=?",
            (digest, session_id),
        )


# ---------------------------------------------------------------- run / file 表


def begin_import_run(conn: sqlite3.Connection, run_id: str, adapter_id: str,
                     adapter_version: int, started_at: str) -> None:
    conn.execute(
        "INSERT INTO import_runs(run_id, adapter_id, adapter_version,"
        " started_at, result) VALUES(?,?,?,?,?)",
        (run_id, adapter_id, adapter_version, started_at, "running"))


def finish_import_run(conn: sqlite3.Connection, run_id: str, *,
                      finished_at: str, result: str,
                      scanned_files: int | None = None,
                      imported_rows: int | None = None,
                      failed_rows: int | None = None,
                      warnings_json: str | None = None) -> None:
    cols: dict[str, object] = {
        "finished_at": finished_at, "result": result,
        "scanned_files": scanned_files, "imported_rows": imported_rows,
        "failed_rows": failed_rows, "warnings_json": warnings_json,
    }
    parts = ", ".join(f"{k}=?" for k, v in cols.items() if v is not None)
    args = [v for v in cols.values() if v is not None]
    conn.execute(f"UPDATE import_runs SET {parts} WHERE run_id=?",
                 (*args, run_id))


def register_import_files(conn: sqlite3.Connection,
                          rows: list[tuple[str, int, float]]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO import_files(path, size, mtime) VALUES(?,?,?)",
        rows)


def reset_import_file(conn: sqlite3.Connection, *, size: int, mtime: float,
                      path: str) -> None:
    conn.execute(
        "UPDATE import_files SET size=?, mtime=?, offset=0, rows=0,"
        " done=0, error=NULL WHERE path=?", (size, mtime, path))


def clear_import_file_error(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(
        "UPDATE import_files SET offset=0, rows=0, done=0, error=NULL"
        " WHERE path=?", (path,))


def save_import_file_state(conn: sqlite3.Connection, *, path: str, offset: int,
                           rows: int, done: int, last_run_id: str,
                           error: str | None) -> None:
    conn.execute(
        "UPDATE import_files SET offset=?, rows=?, done=?, last_run_id=?,"
        " error=? WHERE path=?",
        (offset, rows, done, last_run_id, error, path))
