"""catalog 运行/文件 API：行为与 importer 原裸 SQL 等价。"""
from __future__ import annotations

import pytest

from sessionpack import catalog


@pytest.fixture()
def conn(tmp_path):
    c = catalog.connect(tmp_path / "c.db")
    catalog.init_schema(c)
    yield c
    c.close()


def test_begin_and_finish_import_run_partial_update(conn):
    catalog.begin_import_run(conn, "r1", "trae-memory", "1", "2026-01-01T00:00:00")
    conn.commit()
    row = conn.execute(
        "SELECT * FROM import_runs WHERE run_id='r1'").fetchone()
    assert row["result"] == "running"
    # 只更新传入非 None 的列
    catalog.finish_import_run(conn, "r1", finished_at="2026-01-01T00:01:00",
                              result="ok", imported_rows=5)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM import_runs WHERE run_id='r1'").fetchone()
    assert row["result"] == "ok"
    assert row["imported_rows"] == 5
    # 未传入的列不被改写（schema: scanned_files NOT NULL DEFAULT 0）
    assert row["scanned_files"] == 0
    # 二次调用补列不覆盖已写值
    catalog.finish_import_run(conn, "r1", finished_at="2026-01-01T00:02:00",
                              result="ok", failed_rows=2)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM import_runs WHERE run_id='r1'").fetchone()
    assert row["imported_rows"] == 5 and row["failed_rows"] == 2


def test_import_files_api_roundtrip(conn):
    catalog.register_import_files(conn, [("/x/a.jsonl", 10, 1.5)])
    rows = catalog.load_import_files(conn)
    assert rows["/x/a.jsonl"]["size"] == 10
    catalog.save_import_file_state(
        conn, path="/x/a.jsonl", offset=3, rows=3, done=1,
        last_run_id="r1", error=None)
    catalog.reset_import_file(conn, size=20, mtime=2.5, path="/x/a.jsonl")
    row = catalog.load_import_files(conn)["/x/a.jsonl"]
    assert (row["size"], row["offset"], row["done"], row["error"]) == (20, 0, 0, None)
    catalog.save_import_file_state(
        conn, path="/x/a.jsonl", offset=1, rows=1, done=0,
        last_run_id="r1", error="boom")
    catalog.clear_import_file_error(conn, "/x/a.jsonl")
    row = catalog.load_import_files(conn)["/x/a.jsonl"]
    assert row["error"] is None and row["offset"] == 0 and row["done"] == 0
    catalog.register_import_files(conn, [("/x/a.jsonl", 99, 9.9)])  # OR IGNORE
    assert catalog.load_import_files(conn)["/x/a.jsonl"]["size"] == 20
