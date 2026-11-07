"""catalog 的连接、建表与元信息。

可写连接是 connect()，只读连接是 connect_readonly()：
后者把散落在 cli/diagnostics 的 mode=ro 样板收口到一处。

版本门禁（Spec 7 VER-1）：user_version 标记索引形状版本，
init_schema 对"表齐全但缺列"的史前库与"库比工具新"两种情况
fail-closed，导向 sessionpack rebuild --from-archives 重建。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")

SCHEMA_VERSION = 1

_REBUILD_HINT = "运行 sessionpack rebuild --from-archives 重建索引"


class CatalogVersionError(RuntimeError):
    """库的 user_version 与当前工具不匹配（过新，或过旧且无迁移路径）。"""


class CatalogSchemaError(RuntimeError):
    """库的表形状不符合当前 schema（如史前库缺列），只能重建。"""


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


# v1 核心表的必需列（逐字取自 SCHEMA 现形状），缺列即形状不符
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("project_id", "display_name", "local_path_hint",
                 "source_kind", "updated_at"),
    "sessions": ("session_id", "project_id", "source_kind", "title",
                 "summary", "created_at", "updated_at", "content_hash",
                 "status"),
    "messages": ("session_id", "message_id", "seq", "ts", "intent",
                 "outcome", "actions", "learned", "meta_json", "body"),
}


def schema_version(conn: sqlite3.Connection) -> int:
    """读 user_version；未打戳的史前库与全新库返回 0。"""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _check_required_columns(conn: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        present = {row[1] for row in conn.execute(
            "PRAGMA table_info(" + table + ")")}
        if not present:
            continue  # 表不存在（IF NOT EXISTS 刚建出全新表，或库未用到该表）
        missing = [col for col in required if col not in present]
        if missing:
            raise CatalogSchemaError(
                f"索引库形状不符合当前版本：{table} 缺列 "
                + "、".join(missing)
                + f"；{_REBUILD_HINT}")


def init_schema(conn: sqlite3.Connection) -> str:
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
        "session_id UNINDEXED, message_id UNINDEXED, text, tokenize='trigram')"
    )
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise CatalogVersionError(
            f"索引库版本 {version} 比当前工具支持的 {SCHEMA_VERSION} 新，"
            "请升级 sessionpack 后重试")
    if 0 < version < SCHEMA_VERSION:
        raise CatalogVersionError(
            f"索引库版本 {version} 过旧（当前 {SCHEMA_VERSION}），无迁移路径，"
            f"{_REBUILD_HINT}")
    if version == 0:
        # 史前库/新库：形状校验通过才打戳；缺列即刻抛 CatalogSchemaError
        _check_required_columns(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return tokenizer_of(conn)


def tokenizer_of(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_fts'"
    ).fetchone()
    if row is None:
        return "none"
    return "trigram" if "trigram" in (row[0] or "") else "unicode61"
