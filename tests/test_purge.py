"""Spec 13 物理清除：终态抹除、三道不复活卡、重打包、CLI 冒烟、同步报告。

会话 ID 用 24 位十六进制（归档校验 SESSION_ID_RE 的硬要求）。
_seed/_open/_status 与 tests/test_delete_tombstone.py 同构（不跨测试文件导入，
避免测试互相依赖）。
"""
from __future__ import annotations

from sessionpack import catalog
from sessionpack.catalog.write import (
    purge_session,
    upsert_message,
    upsert_topic,
)
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord
from sessionpack.paths import ensure_layout

SID = "0123456789abcdef01234567"
BODY = "需要彻底抹除的敏感内容"


def _seed(conn, session_id, *, project_id="p1", body=BODY):
    catalog.upsert_project(conn, ProjectRecord(
        project_id=project_id, display_name=project_id,
        local_path_hint=None, source_kind="session"))
    catalog.upsert_topic(conn, TopicRecord(
        session_id=session_id, project_id=project_id, source_kind="session",
        topic_time="2026-11-08T10:00:00",
        body=f"{session_id} 的主题（关键词：机密词）"))
    catalog.upsert_message(conn, MessageRecord(
        session_id=session_id, message_id=f"{session_id}-m1", seq=1,
        ts="2026-11-08T10:00:01", intent=None, outcome=None,
        actions=(), learned=(), meta=None, body=body,
        project_id=project_id, source_kind="session"))
    conn.commit()


def _open(tmp_path, name):
    lay = ensure_layout(tmp_path / name)
    conn = catalog.connect(lay.catalog)
    catalog.init_schema(conn)
    return tmp_path / name, conn


def _status(conn, session_id):
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["status"] if row else None


def _purge_setup(tmp_path, name="root"):
    """建库、播种、delete 到 deleted 态，返回 (root, conn)。"""
    root, conn = _open(tmp_path, name)
    _seed(conn, SID)
    assert catalog.mark_session_deleted(
        conn, SID, now="2026-11-08T11:00:00")
    conn.commit()
    return root, conn


def test_purge_erases_content_and_marks_terminal(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is True
    conn.commit()
    row = conn.execute(
        "SELECT status, title, summary, content_hash, updated_at "
        "FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["status"] == "purged"
    assert row["title"] is None and row["summary"] is None
    assert row["content_hash"] is None
    assert row["updated_at"] == "2026-11-08T12:00:00"
    # sessions 行保留（purge 卡载体）；消息与 FTS 物理删除
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 0


def test_purge_hides_everything(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00")
    conn.commit()
    assert catalog.get_session(conn, SID) is None
    assert catalog.list_sessions(conn) == []
    assert catalog.search_messages(conn, "敏感") == []
    assert catalog.search_messages(conn, BODY) == []
    assert catalog.get_messages(conn, SID) == []
    assert catalog.get_projects(conn)[0]["session_count"] == 0
    # 原始状态查询仍可达（undo/purge 判家）
    assert catalog.get_session_status(conn, SID) == "purged"


def test_purge_only_from_deleted(tmp_path):
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    # active 态直接 purge：拒绝且不抹数据
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is False
    assert _status(conn, SID) == "active"
    assert len(catalog.get_messages(conn, SID)) == 1
    # deleted → purged → 再 purge：幂等 False，updated_at 不变
    catalog.mark_session_deleted(conn, SID, now="2026-11-08T11:00:00")
    conn.commit()
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is True
    conn.commit()
    assert purge_session(conn, SID, now="2026-11-08T13:00:00") is False
    row = conn.execute(
        "SELECT updated_at FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["updated_at"] == "2026-11-08T12:00:00"
    # 防降级：purged 后 delete 不再改写状态
    assert catalog.mark_session_deleted(conn, SID, now="2026-11-08T14:00:00") is False
    assert _status(conn, SID) == "purged"
    conn.close()


def test_no_revive_via_upsert(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00")
    conn.commit()
    # 守卫①：purged 会话上的主题/消息写入全部 no-op
    topic_result = upsert_topic(conn, TopicRecord(
        session_id=SID, project_id="p1", source_kind="session",
        topic_time="2026-11-09T10:00:00", body="迟到的主题"))
    assert topic_result.inserted is False and topic_result.session_created is False
    msg_result = upsert_message(conn, MessageRecord(
        session_id=SID, message_id=f"{SID}-m2", seq=2,
        ts="2026-11-09T10:00:01", intent=None, outcome=None,
        actions=(), learned=(), meta=None, body="迟到的正文",
        project_id="p1", source_kind="session"))
    assert msg_result.inserted is False and msg_result.session_created is False
    conn.commit()
    assert _status(conn, SID) == "purged"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    conn.close()
