"""connect_readonly 的三条硬行为：只读、不建文件、row_factory 可用。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sessionpack import catalog


def test_connect_readonly_rejects_write(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    rw = catalog.connect(db)
    try:
        catalog.init_schema(rw)
    finally:
        rw.close()

    ro = catalog.connect_readonly(db)
    try:
        assert catalog.has_schema(ro) is True
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO projects(project_id, display_name, source_kind)"
                " VALUES('aa', 'bb', 'trae')"
            )
    finally:
        ro.close()


def test_connect_readonly_does_not_create_file(tmp_path: Path) -> None:
    missing = tmp_path / "sub" / "absent.db"
    with pytest.raises(sqlite3.Error):
        catalog.connect_readonly(missing)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_connect_readonly_uses_row_factory(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    conn = catalog.connect(db)
    try:
        catalog.init_schema(conn)
    finally:
        conn.close()

    ro = catalog.connect_readonly(db)
    try:
        row = ro.execute("SELECT 1 AS one").fetchone()
        assert row["one"] == 1
    finally:
        ro.close()
