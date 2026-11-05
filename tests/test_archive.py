import json
import zipfile
from pathlib import Path

import pytest

from sessionpack import archive, catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


@pytest.fixture
def filled_db(memory_tree: Path, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return db


def _rewrite(src: Path, dst: Path, mutate: dict[str, bytes] | None = None,
             drop: set[str] | None = None, add: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            if drop and name in drop:
                continue
            data = mutate[name] if mutate and name in mutate else zin.read(name)
            zout.writestr(name, data)
        for name, data in (add or {}).items():
            zout.writestr(name, data)


def test_build_archive_has_manifest_and_is_stable(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    first = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA",
                                  now="2026-01-03T00:00:00")
    second = archive.build_archive(conn, tmp_path / "b", "-d-Work-RepoA",
                                   now="2026-01-03T00:00:00")
    assert first.archive_id == second.archive_id
    assert first.sessions == 1 and first.messages == 2
    with zipfile.ZipFile(first.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["archive_id"] == first.archive_id
    assert manifest["format"] == "sessionpack-archive"
    assert len(manifest["files"]) == 2
    assert archive.verify_archive(first.path).ok is True


def test_verify_detects_hash_mismatch(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"sessions/" + "a" * 24 + "/messages.ndjson": b"x"})
    result = archive.verify_archive(bad)
    assert result.ok is False
    assert "E_HASH_MISMATCH" in {issue.code for issue in result.issues}


def test_verify_detects_missing_manifest(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, drop={"manifest.json"})
    result = archive.verify_archive(bad)
    assert [issue.code for issue in result.issues] == ["E_MANIFEST_MISSING"]


def test_verify_detects_unregistered_file(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, add={"extra.txt": b"hello"})
    codes = {issue.code for issue in archive.verify_archive(bad).issues}
    assert "E_UNREGISTERED_FILE" in codes


def test_verify_rejects_path_traversal(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    with zipfile.ZipFile(built.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["files"].append({"path": "../evil.txt", "sha256": "0" * 64, "size": 0})
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad,
             mutate={"manifest.json": json.dumps(manifest).encode("utf-8")},
             add={"../evil.txt": b"x"})
    result = archive.verify_archive(bad)
    assert "E_PATH_ILLEGAL" in {issue.code for issue in result.issues}


def test_import_archive_roundtrip(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    fresh_db = tmp_path / "fresh.sqlite"
    fresh = catalog.connect(fresh_db)
    catalog.init_schema(fresh)
    result = archive.import_archive(fresh, built.path)
    assert result.sessions == 1 and result.messages == 2
    assert result.messages_inserted == 2
    again = archive.import_archive(fresh, built.path)
    assert again.messages_inserted == 0
    assert catalog.stats(fresh)["messages"] == 2


def test_import_refuses_tampered_archive(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"sessions/" + "a" * 24 + "/messages.ndjson": b"x"})
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
