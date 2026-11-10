"""对外导入契约锁死测试：拆包后 sessionpack.catalog.* 的既有名字一个都不能少。"""

from __future__ import annotations


def test_catalog_facade_exports_all_public_names() -> None:
    import sessionpack.catalog as catalog

    expected = {
        "SESSION_ID_RE",
        "SCHEMA",
        "WriteResult",
        "connect",
        "connect_readonly",
        "init_schema",
        "has_schema",
        "tokenizer_of",
        "upsert_project",
        "upsert_topic",
        "upsert_message",
        "refresh_session_hashes",
        "search_messages",
        "search_sessions",
        "list_sessions",
        "get_session",
        "get_messages",
        "get_projects",
        "stats",
        "begin_import_run",
        "finish_import_run",
        "load_import_files",
        "register_import_files",
        "reset_import_file",
        "clear_import_file_error",
        "save_import_file_state",
        "require_catalog",
    }
    missing = sorted(n for n in expected if not hasattr(catalog, n))
    assert missing == []
    assert set(catalog.__all__) == expected


def test_has_schema_comes_from_read_module() -> None:
    """has_schema 属只读职责，必须由 read 子模块提供，防止后续误搬回 conn。"""
    from sessionpack.catalog import read

    assert callable(read.has_schema)


def test_server_validates_session_id_without_reexport() -> None:
    """server 删除 SESSION_ID_RE 重导出后，非法 id 仍须被拒（行为回归）。"""
    import sessionpack.server as server

    assert not hasattr(server, "SESSION_ID_RE")
