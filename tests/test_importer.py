from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sessionpack import catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source

REPO_ROOT = Path(__file__).resolve().parents[1]
CRASH_HELPER = REPO_ROOT / "tests" / "helpers" / "crash_import.py"


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _counts(path: Path) -> dict[str, int]:
    conn = catalog.connect(path)
    result = catalog.stats(conn)
    conn.close()
    return result


def test_import_populates_catalog(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    stats = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert stats.files_declared == 4
    assert stats.files_processed == 4
    assert stats.messages_inserted == 4
    assert stats.topics_applied == 2
    assert stats.sessions_created == 2
    assert stats.rows_failed == 2
    assert all(":" in failure for failure in stats.failures)
    assert not any(str(memory_tree) in failure for failure in stats.failures)
    assert _counts(db) == {
        "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
    }


def test_second_import_is_idempotent(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    again = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert again.files_skipped == 4
    assert again.messages_inserted == 0
    assert _counts(db)["messages"] == 4


def test_reparse_does_not_duplicate(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    again = import_source(TraeMemoryAdapter(), memory_tree, db, reparse=True)
    assert again.files_skipped == 0
    assert again.messages_inserted == 0
    assert again.rows_skipped == 4
    assert again.sessions_created == 0
    assert _counts(db)["messages"] == 4


def test_stop_then_resume_matches_full_import(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    partial = import_source(TraeMemoryAdapter(), memory_tree, db, stop_after_rows=2)
    assert partial.stopped is True
    assert partial.messages_inserted == 2
    rest = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert rest.stopped is False
    assert _counts(db) == {
        "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
    }


def test_appended_rows_are_imported_after_rewrite(
    memory_tree: Path, tmp_path: Path
) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    target = next(memory_tree.glob("projects/*/20260102/session_memory_*.jsonl"))
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"message_id": "m3", "message_summary_time": "2026-01-02 10:00:00",'
            ' "intent": "追加", "outcome": "新增一条", "actions": [], "learned": []}\n'
        )
    again = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert again.messages_inserted == 1
    assert _counts(db)["messages"] == 5


def test_small_batch_still_idempotent(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    first = import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    assert first.batches >= 6
    second = import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    assert second.messages_inserted == 0
    assert _counts(db)["messages"] == 4


def test_project_and_date_filters(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    stats = import_source(
        TraeMemoryAdapter(), memory_tree, db,
        project_filter="-d-Work-RepoB", since="2026-01-02", until="2026-01-02",
    )
    assert stats.files_declared == 1
    assert _counts(db)["messages"] == 0
    assert _counts(db)["sessions"] == 1


def test_hard_kill_keeps_committed_rows_and_resumes(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(memory_tree), str(db), "4", "1"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 9, proc.stderr
    conn = catalog.connect(db)
    try:
        partial = catalog.stats(conn)["messages"]
    finally:
        conn.close()
    assert 0 < partial < 4

    rest = import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    conn = catalog.connect(db)
    try:
        assert catalog.stats(conn)["messages"] == 4
        assert catalog.stats(conn)["fts_rows"] == 4
        done = conn.execute(
            "SELECT COUNT(*) FROM import_files WHERE done=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert done == 4
    assert rest.messages_inserted == 4 - partial


def test_batch_survives_hard_kill_without_loss_or_duplicates(
    memory_tree: Path, tmp_path: Path
) -> None:
    db = tmp_path / "catalog.sqlite"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(memory_tree), str(db), "3", "2000"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 9, proc.stderr
    conn = catalog.connect(db)
    try:
        assert catalog.stats(conn)["messages"] == 0
    finally:
        conn.close()

    import_source(TraeMemoryAdapter(), memory_tree, db)
    assert _counts(db) == {
        "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
    }


def test_import_never_modifies_source_tree(memory_tree: Path, tmp_path: Path) -> None:
    before = _snapshot(memory_tree)
    import_source(TraeMemoryAdapter(), memory_tree, tmp_path / "catalog.sqlite")
    assert _snapshot(memory_tree) == before


def test_midfile_oserror_not_marked_done(
    memory_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "catalog.sqlite"
    failing = next(memory_tree.glob("projects/*/20260102/session_memory_*.jsonl"))
    rel = failing.relative_to(memory_tree).as_posix()
    real_read = TraeMemoryAdapter.read_file
    boom = {"armed": True}

    def flaky(
        self: TraeMemoryAdapter, ref, start_offset: int = 0, start_seq: int = 0
    ):
        for item in real_read(self, ref, start_offset, start_seq):
            yield item
            if boom["armed"] and ref.path == failing:
                raise OSError("simulated mid-read failure")

    monkeypatch.setattr(TraeMemoryAdapter, "read_file", flaky)
    first = import_source(TraeMemoryAdapter(), memory_tree, db)

    assert any(rel in failure for failure in first.failures)
    conn = catalog.connect(db)
    try:
        row = conn.execute(
            "SELECT * FROM import_files WHERE path=?", (str(failing),)
        ).fetchone()
        mid_counts = catalog.stats(conn)
    finally:
        conn.close()
    assert row["done"] == 0
    assert 0 < row["offset"] < failing.stat().st_size
    assert "simulated mid-read failure" in (row["error"] or "")
    assert mid_counts["messages"] == 3

    boom["armed"] = False
    again = import_source(TraeMemoryAdapter(), memory_tree, db)

    assert again.messages_inserted == 1
    conn = catalog.connect(db)
    try:
        row = conn.execute(
            "SELECT * FROM import_files WHERE path=?", (str(failing),)
        ).fetchone()
        assert catalog.stats(conn) == {
            "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
        }
        rows = conn.execute(
            "SELECT message_id, seq FROM messages WHERE session_id=? ORDER BY seq",
            ("b" * 24,),
        ).fetchall()
    finally:
        conn.close()
    assert row["done"] == 1
    assert [tuple(r) for r in rows] == [("m1", 1), ("m2", 2)]


