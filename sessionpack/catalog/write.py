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


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """从 messages.body 全量重建 message_fts（单事务）。

    派生索引修复：不另设备份；调用方在异常时 rollback，
    message_fts 即维持原状。返回重建后的 FTS 行数。
    """
    rows = conn.execute(
        "SELECT session_id, message_id, body FROM messages ORDER BY session_id, seq"
    ).fetchall()
    conn.execute("DELETE FROM message_fts")
    conn.executemany(
        "INSERT INTO message_fts(session_id, message_id, text) VALUES(?,?,?)",
        [(r["session_id"], r["message_id"], r["body"] or "") for r in rows],
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]


def clear_derived_rows(conn: sqlite3.Connection) -> None:
    """清空 sessions/messages/message_fts 三表（Spec 7 §5.2 fresh 重建前置）。

    projects 与 import_runs/import_files 保留——项目元信息无害，
    导入进度由调用方决定去留（fresh 走全新全量重导，旧进度无意义但无害）。
    """
    conn.execute("DELETE FROM message_fts")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM sessions")
    conn.commit()


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
        "SELECT updated_at, status FROM sessions WHERE session_id=?",
        (rec.session_id,),
    ).fetchone()
    if existing is not None and existing["status"] == "purged":
        # 守卫①：purged 终态不可复活（import/rebuild 旧数据也走这条路）
        return WriteResult(False, False)
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
    guarded = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?", (rec.session_id,)
    ).fetchone()
    if guarded is not None and guarded["status"] == "purged":
        return WriteResult(False, False)
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


def mark_session_deleted(conn: sqlite3.Connection, session_id: str, *,
                         now: str) -> bool:
    """软删除落墓碑：status='deleted'，消息与 FTS 不动（Spec 12 D1）。

    会话不存在返回 False；重复标记幂等返回 True。
    """
    cur = conn.execute(
        "UPDATE sessions SET status='deleted', updated_at=? "
        "WHERE session_id=? AND status != 'purged'",
        (now, session_id),
    )
    return cur.rowcount > 0


def purge_session(conn, session_id: str, *, now: str) -> bool:
    """物理清除：sessions 行置 purged 终态并清空内容字段，抹掉消息与 FTS。

    仅 deleted 态可进（G2）：True=本次真清除；purged 幂等、active 拒绝均返 False。
    sessions 行保留，作为 purge 卡载体与 install 判家依据。
    与 Spec D2 语句顺序对调：先 UPDATE 判态再抹数据——active 误调时必须
    原样拒掉，不能先把消息删了再发现状态不对。
    """
    cur = conn.execute(
        "UPDATE sessions SET status='purged', title=NULL, summary=NULL, "
        "content_hash=NULL, updated_at=? "
        "WHERE session_id=? AND status='deleted'",
        (now, session_id),
    )
    if cur.rowcount == 0:
        return False
    conn.execute("DELETE FROM message_fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    return True


def insert_session_purge_marker(conn, *, session_id: str, project_id: str,
                                source_kind: str, created_at: str,
                                updated_at: str) -> None:
    """归档 purge 卡的强制落卡语义（三道卡②的执行端，与墓碑分支对称）。

    行存在 → 抹消息与 FTS、置 purged、清内容字段（覆盖 active/deleted/purged
    三态）；行不存在（新设备）→ 直接 INSERT 终态卡。
    """
    conn.execute("DELETE FROM message_fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    cur = conn.execute(
        "UPDATE sessions SET status='purged', title=NULL, summary=NULL, "
        "content_hash=NULL, updated_at=? WHERE session_id=?",
        (updated_at, session_id),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO sessions(session_id, project_id, source_kind, title, "
            "summary, created_at, updated_at, status) "
            "VALUES(?,?,?,?,?,?,?,'purged')",
            (session_id, project_id, source_kind, None, None,
             created_at, updated_at),
        )


def insert_session_tombstone(conn: sqlite3.Connection, *, session_id: str,
                             project_id: str, source_kind: str,
                             created_at: str, updated_at: str) -> None:
    """墓碑包引用了本库不存在的会话时 INSERT 墓碑行（Spec 12 D4，新设备场景）。

    OR IGNORE 幂等：行已存在（含已墓碑化）时什么都不做。
    """
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_id, source_kind,"
        " title, summary, created_at, updated_at, status)"
        " VALUES(?,?,?,?,?,?,?,'deleted')",
        (session_id, project_id, source_kind, None, None,
         created_at, updated_at),
    )


def restore_session(conn: sqlite3.Connection, session_id: str, *,
                    now: str) -> bool:
    """本机撤销墓碑：仅当前 status='deleted' 才置回 active，否则 False。"""
    cur = conn.execute(
        "UPDATE sessions SET status='active', updated_at=?"
        " WHERE session_id=? AND status='deleted'",
        (now, session_id),
    )
    return cur.rowcount > 0


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
