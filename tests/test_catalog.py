from pathlib import Path

import pytest

from sessionpack import catalog, export
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source
from sessionpack.model import MessageRecord, TopicRecord


def _msg(body: str, message_id: str = "m1", seq: int = 1,
         ts: str = "2026-01-01 10:00:00") -> MessageRecord:
    return MessageRecord(
        session_id="a" * 24, message_id=message_id, seq=seq, ts=ts,
        intent="导入会话", outcome=body, actions=("读取文件",),
        learned=("批量提交更快",), meta=None, body=body,
        project_id="-d-Work-RepoA", source_kind="trae-memory",
    )


def test_init_schema_creates_tables_and_trigram(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    assert catalog.init_schema(conn) == "trigram"
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"projects", "sessions", "messages", "import_runs",
            "import_files", "message_fts"} <= names
    assert catalog.tokenizer_of(conn) == "trigram"


def test_upsert_message_is_idempotent_and_indexed(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    first = catalog.upsert_message(conn, _msg("归档包的校验流程已完成"))
    second = catalog.upsert_message(conn, _msg("归档包的校验流程已完成"))
    assert first == catalog.WriteResult(True, True)
    assert second == catalog.WriteResult(False, False)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 1
    hits = catalog.search_messages(conn, "归档包")
    assert len(hits) == 1 and hits[0]["session_id"] == "a" * 24


def test_search_falls_back_to_like_for_two_char_term(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("校验通过"))
    assert len(catalog.search_messages(conn, "校验")) == 1


def test_topic_keeps_latest_summary_and_does_not_regress(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    base = dict(session_id="a" * 24, project_id="-d-Work-RepoA",
                source_kind="trae-memory")
    newer = catalog.upsert_topic(conn, TopicRecord(
        topic_time="2026-01-02 10:00:00", body="新摘要", **base))
    older = catalog.upsert_topic(conn, TopicRecord(
        topic_time="2026-01-01 10:00:00", body="旧摘要", **base))
    assert newer.session_created is True and older.session_created is False
    row = catalog.get_session(conn, "a" * 24)
    assert row["summary"] == "新摘要"
    assert row["updated_at"] == "2026-01-02 10:00:00"


def test_list_sessions_filters_by_project_and_date(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_topic(conn, TopicRecord(
        session_id="a" * 24, topic_time="2026-01-02 10:00:00", body="新摘要",
        project_id="-d-Work-RepoA", source_kind="trae-memory"))
    assert len(catalog.list_sessions(conn, project_id="-d-Work-RepoA")) == 1
    assert len(catalog.list_sessions(conn, project_id="-d-Work-RepoB")) == 0
    assert len(catalog.list_sessions(conn, date_from="2026-01-02")) == 1
    assert len(catalog.list_sessions(conn, date_to="2026-01-01")) == 0


def test_refresh_session_hashes_is_stable(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文一", "m1", 1))
    catalog.upsert_message(conn, _msg("正文二", "m2", 2))
    catalog.refresh_session_hashes(conn, {"a" * 24})
    first = catalog.get_session(conn, "a" * 24)["content_hash"]
    catalog.refresh_session_hashes(conn, {"a" * 24})
    assert first == catalog.get_session(conn, "a" * 24)["content_hash"]
    assert len(first) == 64


def test_upsert_message_null_ts_keeps_updated_at(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文一", "m1", 1))
    catalog.upsert_message(conn, _msg("正文二", "m2", 2, ts=None))
    assert catalog.get_session(conn, "a" * 24)["updated_at"] == "2026-01-01 10:00:00"


def test_stats_returns_all_counts(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文"))
    assert catalog.stats(conn) == {
        "projects": 0, "sessions": 1, "messages": 1, "fts_rows": 1,
    }


def test_session_id_rejects_trailing_newline() -> None:
    assert catalog.SESSION_ID_RE.fullmatch("a" * 24)
    assert catalog.SESSION_ID_RE.fullmatch("a" * 24 + "\n") is None
    assert catalog.SESSION_ID_RE.fullmatch("a" * 23) is None


def test_export_session_rejects_trailing_newline(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文"))
    with pytest.raises(ValueError):
        export.export_session(conn, "a" * 24 + "\n", tmp_path / "out")


@pytest.fixture
def filled_db_conn(memory_tree: Path, tmp_path: Path):
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return catalog.connect(db)


def test_like_wildcards_are_literal(filled_db_conn, tmp_path) -> None:
    conn = filled_db_conn
    # 库内已有会话正文不含 "%"；搜索字面 "%" 不应命中任何消息
    assert catalog.search_messages(conn, "%") == []
    assert catalog.list_sessions(conn, q="%") == []


def test_list_sessions_source_and_date_filters(filled_db_conn) -> None:
    conn = filled_db_conn
    all_rows = catalog.list_sessions(conn)
    assert catalog.list_sessions(conn, source_kind="trae-memory") == all_rows
    assert catalog.list_sessions(conn, source_kind="none") == []
    later = catalog.list_sessions(conn, date_from="2026-01-02")
    earlier = catalog.list_sessions(conn, date_to="2026-01-01")
    assert {r["session_id"] for r in later} != {r["session_id"] for r in earlier}
    assert later and earlier
    assert catalog.list_sessions(
        conn, date_from="2026-01-02", date_to="2026-01-02"
    ) == later
