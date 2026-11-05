import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sessionpack import archive, catalog, export
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


def test_import_never_modifies_source_tree(memory_tree: Path, tmp_path: Path) -> None:
    before = _snapshot(memory_tree)
    import_source(TraeMemoryAdapter(), memory_tree, tmp_path / "catalog.sqlite")
    assert _snapshot(memory_tree) == before


def test_hard_kill_then_resume_completes(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(memory_tree), str(db), "4", "1"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 9, proc.stderr
    conn = catalog.connect(db)
    partial = catalog.stats(conn)["messages"]
    conn.close()
    assert 0 < partial < 4

    import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    conn = catalog.connect(db)
    try:
        assert catalog.stats(conn)["messages"] == 4
        done = conn.execute("SELECT COUNT(*) FROM import_files WHERE done=1").fetchone()[0]
        assert done == 4
    finally:
        conn.close()


def test_archive_roundtrip_and_export_is_stable(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    conn = catalog.connect(db)
    try:
        project = catalog.get_projects(conn)[0]
        built = archive.build_archive(conn, tmp_path / "archives", project["project_id"])
        before = export.export_project(conn, project["project_id"], tmp_path / "before")
    finally:
        conn.close()

    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    try:
        imported = archive.import_archive(fresh, built.path)
        after = export.export_project(fresh, project["project_id"], tmp_path / "after")
    finally:
        fresh.close()

    assert imported.sessions == built.sessions
    assert imported.messages == built.messages
    assert [p.name for p in before.paths] == [p.name for p in after.paths]
    for left, right in zip(before.paths, after.paths):
        assert left.read_text(encoding="utf-8") == right.read_text(encoding="utf-8")
    assert archive.verify_archive(built.path).ok
