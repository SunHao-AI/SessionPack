"""catalog 包门面：保持拆分前 sessionpack.catalog.X 的全部可导入名字。

子模块归位规则（与 scripts/lint_imports.py 的 LAYER_OF 子模块表配套）：
conn=连接/建表/元信息，read=只读查询，write=写入与导入进度。
"""

from __future__ import annotations

from sessionpack.catalog.conn import (
    SCHEMA,
    SESSION_ID_RE,
    connect,
    connect_readonly,
    init_schema,
    tokenizer_of,
)
from sessionpack.catalog.read import (
    get_messages,
    get_projects,
    get_session,
    # 墓碑三件套 + 原始 status 查询（Spec 12）：门面可达但不进 __all__，
    # 对外契约集合由 tests/test_import_contracts.py 冻结，不得扩充。
    get_session_status,  # noqa: F401
    has_schema,
    list_sessions,
    load_import_files,
    require_catalog,
    search_messages,
    stats,
)
from sessionpack.catalog.write import (
    WriteResult,
    begin_import_run,
    clear_import_file_error,
    finish_import_run,
    insert_session_purge_marker,  # noqa: F401
    insert_session_tombstone,  # noqa: F401
    mark_session_deleted,  # noqa: F401
    purge_session,  # noqa: F401
    refresh_session_hashes,
    register_import_files,
    reset_import_file,
    restore_session,  # noqa: F401
    save_import_file_state,
    upsert_message,
    upsert_project,
    upsert_topic,
)

__all__ = [
    "SCHEMA",
    "SESSION_ID_RE",
    "WriteResult",
    "begin_import_run",
    "clear_import_file_error",
    "connect",
    "connect_readonly",
    "finish_import_run",
    "get_messages",
    "get_projects",
    "get_session",
    "has_schema",
    "init_schema",
    "list_sessions",
    "load_import_files",
    "refresh_session_hashes",
    "register_import_files",
    "require_catalog",
    "reset_import_file",
    "save_import_file_state",
    "search_messages",
    "stats",
    "tokenizer_of",
    "upsert_message",
    "upsert_project",
    "upsert_topic",
]
