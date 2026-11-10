"""catalog 的只读查询：检索、列表、详情、统计与只读表加载。

本模块只做 SELECT；SQL 守卫会拦住任何误写的 INSERT/UPDATE/DELETE。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sessionpack.catalog.conn import connect_readonly, tokenizer_of


def has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    return bool(row and row[0])


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
                " JOIN sessions s ON s.session_id=m.session_id"
                " AND s.status = 'active'"
                " WHERE message_fts MATCH ? LIMIT ?",
                (_fts_expr(term), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    return conn.execute(
        "SELECT session_id, message_id, body FROM messages"
        " WHERE body LIKE ? ESCAPE '\\'"
        " AND session_id IN (SELECT session_id FROM sessions"
        " WHERE status = 'active') LIMIT ?",
        (_like(term), limit),
    ).fetchall()


def search_sessions(
    conn: sqlite3.Connection, term: str, limit: int = 50
) -> list[sqlite3.Row]:
    """会话级检索：标题/摘要 LIKE 匹配（墓碑对用户视图隐藏）。

    与 search_messages 互补——短词与停用词场景下消息 FTS 无果，
    但用户记往往只记得会话标题里的词。
    """
    term = term.strip()
    if not term:
        return []
    like = _like(term)
    return conn.execute(
        "SELECT s.session_id, s.title, s.summary,"
        " COALESCE(s.updated_at, s.created_at, '') AS updated"
        " FROM sessions s WHERE s.status = 'active'"
        " AND (s.title LIKE ? ESCAPE '\\' OR s.summary LIKE ? ESCAPE '\\')"
        " ORDER BY updated DESC LIMIT ?",
        (like, like, limit),
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
    # 墓碑会话对用户视图隐藏（Spec 12 D2）
    clauses: list[str] = ["s.status = 'active'"]
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
        " WHERE s.session_id=? AND s.status = 'active'",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def get_session_status(conn: sqlite3.Connection, session_id: str) -> str | None:
    """不过滤墓碑的原始 status；会话不存在返回 None（delete --undo 判家用）。"""
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return row["status"] if row else None


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
        " AND s.status = 'active'"
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


def reconcile_fts(conn: sqlite3.Connection) -> dict:
    """FTS 行存在性对账（Spec 7 FTS-1），只判存在性、不比对正文。

    message_fts 未建（tokenizer_of 为 "none"）不算漂移，与 stats 现状语义
    一致；计数相等走快速路径（两侧都从 messages 单调追加，等值时错位概率
    可忽略，这是 doctor 的常开判据）；计数不等才付 set 差集成本拆精确值
    （逐行 SQL 对账在 UNINDEXED 列上是 O(N×M)，实测 >2 分钟，排除）。
    """
    if tokenizer_of(conn) == "none":
        return {"messages": 0, "fts_rows": 0, "fts_missing": 0,
                "fts_extra": 0, "drift": False}
    messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    fts_rows = conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
    missing = extra = 0
    if messages != fts_rows:
        msg_keys = {(row[0], row[1]) for row in conn.execute(
            "SELECT session_id, message_id FROM messages")}
        fts_keys = {(row[0], row[1]) for row in conn.execute(
            "SELECT session_id, message_id FROM message_fts")}
        missing = len(msg_keys - fts_keys)
        extra = len(fts_keys - msg_keys)
    return {"messages": messages, "fts_rows": fts_rows,
            "fts_missing": missing, "fts_extra": extra,
            "drift": missing > 0 or extra > 0}


def load_import_files(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["path"]: row
            for row in conn.execute("SELECT * FROM import_files")}


def require_catalog(path: Path) -> bool:
    """索引文件存在且已建表则 True；任何打开/查询失败都视为未就绪。"""
    if not path.is_file():
        return False
    try:
        conn = connect_readonly(path)
    except sqlite3.Error:
        return False
    try:
        return has_schema(conn)
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
