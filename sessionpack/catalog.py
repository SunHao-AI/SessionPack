from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord

SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id      TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  local_path_hint TEXT,
  source_kind     TEXT NOT NULL,
  updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id  TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  title       TEXT,
  summary     TEXT,
  created_at  TEXT,
  updated_at  TEXT,
  content_hash TEXT,
  status      TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  ts         TEXT,
  intent     TEXT,
  outcome    TEXT,
  actions    TEXT,
  learned    TEXT,
  meta_json  TEXT,
  body       TEXT,
  PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS import_runs (
  run_id         TEXT PRIMARY KEY,
  adapter_id     TEXT NOT NULL,
  adapter_version INTEGER NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  result         TEXT NOT NULL,
  scanned_files  INTEGER NOT NULL DEFAULT 0,
  imported_rows  INTEGER NOT NULL DEFAULT 0,
  failed_rows    INTEGER NOT NULL DEFAULT 0,
  warnings_json  TEXT
);
CREATE TABLE IF NOT EXISTS import_files (
  path        TEXT PRIMARY KEY,
  size        INTEGER NOT NULL,
  mtime       REAL NOT NULL,
  offset      INTEGER NOT NULL DEFAULT 0,
  rows        INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  last_run_id TEXT,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


class WriteResult(NamedTuple):
    inserted: bool
    session_created: bool


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> str:
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
        "session_id UNINDEXED, message_id UNINDEXED, text, tokenize='trigram')"
    )
    conn.commit()
    return tokenizer_of(conn)


def has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    return bool(row and row[0])


def tokenizer_of(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_fts'"
    ).fetchone()
    if row is None:
        return "none"
    return "trigram" if "trigram" in (row[0] or "") else "unicode61"


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


def _fts_expr(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_messages(
    conn: sqlite3.Connection, term: str, limit: int = 50
) -> list[sqlite3.Row]:
    term = term.strip()
    if not term:
        return []
    if len(term) >= 3:
        try:
            return conn.execute(
                "SELECT m.session_id, m.message_id, m.body FROM message_fts"
                " JOIN messages m ON m.session_id=message_fts.session_id"
                " AND m.message_id=message_fts.message_id"
                " WHERE message_fts MATCH ? LIMIT ?",
                (_fts_expr(term), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    return conn.execute(
        "SELECT session_id, message_id, body FROM messages"
        " WHERE body LIKE ? ESCAPE '\\' LIMIT ?",
        (_like(term), limit),
    ).fetchall()


def list_sessions(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    source_kind: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int | None = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if project_id:
        clauses.append("s.project_id = ?")
        params.append(project_id)
    if source_kind:
        clauses.append("s.source_kind = ?")
        params.append(source_kind)
    day = "substr(COALESCE(s.updated_at, s.created_at, ''), 1, 10)"
    if date_from:
        clauses.append(f"{day} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{day} <= ?")
        params.append(date_to)
    if q:
        term = q.strip()
        if len(term) >= 3:
            clauses.append(
                "s.session_id IN (SELECT session_id FROM message_fts"
                " WHERE message_fts MATCH ?)"
            )
            params.append(_fts_expr(term))
        else:
            clauses.append(
                "(s.title LIKE ? ESCAPE '\\' OR s.summary LIKE ? ESCAPE '\\'"
                " OR s.session_id IN (SELECT session_id FROM messages"
                " WHERE body LIKE ? ESCAPE '\\'))"
            )
            params.extend([_like(term)] * 3)
    sql = (
        "SELECT s.*, p.display_name FROM sessions s"
        " LEFT JOIN projects p ON p.project_id = s.project_id"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(s.updated_at, s.created_at, '') DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT s.*, p.display_name FROM sessions s"
        " LEFT JOIN projects p ON p.project_id = s.project_id"
        " WHERE s.session_id=?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def get_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY seq, message_id",
        (session_id,),
    )
    return [dict(row) for row in rows]


def get_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, COUNT(s.session_id) AS session_count FROM projects p"
        " LEFT JOIN sessions s ON s.project_id = p.project_id"
        " GROUP BY p.project_id ORDER BY p.display_name"
    )
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "fts_rows": conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0],
    }


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


def load_import_files(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["path"]: row
            for row in conn.execute("SELECT * FROM import_files")}


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
