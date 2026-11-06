"""catalog 的连接、建表与元信息。

可写连接是 connect()，只读连接是 connect_readonly()：
后者把散落在 cli/diagnostics 的 mode=ro 样板收口到一处。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

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


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """只读连接（URI mode=ro）：不建目录、不建库。

    文件缺失/非 sqlite 文件/权限不足都抛 sqlite3.Error，由调用方捕获。
    """
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> str:
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
        "session_id UNINDEXED, message_id UNINDEXED, text, tokenize='trigram')"
    )
    conn.commit()
    return tokenizer_of(conn)


def tokenizer_of(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_fts'"
    ).fetchone()
    if row is None:
        return "none"
    return "trigram" if "trigram" in (row[0] or "") else "unicode61"
