import json
from pathlib import Path

from sessionpack import catalog, export
from sessionpack.model import MessageRecord, ProjectRecord


def test_render_public_both_formats(tmp_path: Path) -> None:
    """render 公开：markdown 含标题与消息体，json 为合法 JSON。"""
    conn = catalog.connect(tmp_path / "c.db")
    catalog.init_schema(conn)
    catalog.upsert_project(conn, ProjectRecord(
        project_id="proj", display_name="Proj",
        local_path_hint="", source_kind="trae-memory"))
    catalog.upsert_message(conn, MessageRecord(
        session_id="a" * 24, message_id="m1", seq=1, ts="2026-01-01T00:00:00",
        intent=None, outcome=None, actions=(), learned=(), meta=None,
        body="hello body", project_id="proj", source_kind="trae-memory"))
    session = catalog.get_session(conn, "a" * 24)
    messages = catalog.get_messages(conn, "a" * 24)

    md = export.render(session, messages, "markdown")
    assert md.lstrip().startswith("#")
    assert "hello body" in md
    js = json.loads(export.render(session, messages, "json"))
    assert js["messages"][0]["body"] == "hello body"
