import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sessionpack import archive, catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source

SID = "a" * 24
META_NAME = f"sessions/{SID}/meta.json"
NDJSON_NAME = f"sessions/{SID}/messages.ndjson"


@pytest.fixture
def filled_db(memory_tree: Path, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return db


def _rebuild(src: Path, dst: Path, mutate: dict[str, bytes] | None = None,
             drop: set[str] | None = None) -> None:
    """删改成员后重算 manifest 哈希与 archive_id，使 verify 仍然通过。"""
    with zipfile.ZipFile(src) as zin:
        payload = {name: zin.read(name) for name in zin.namelist()}
    for name in drop or ():
        payload.pop(name)
    payload.update(mutate or {})
    manifest = json.loads(payload["manifest.json"])
    manifest["files"] = [e for e in manifest["files"] if e["path"] in payload]
    for entry in manifest["files"]:
        data = payload[entry["path"]]
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["size"] = len(data)
    manifest["archive_id"] = archive.compute_archive_id(manifest["files"])
    payload["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(dst, "w") as zout:
        for name, data in payload.items():
            zout.writestr(name, data)


def _running_runs(conn) -> list[str]:
    return [row["result"] for row in
            conn.execute("SELECT result FROM import_runs WHERE result='running'")]


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
    hits = catalog.search_messages(fresh, "校验归档")
    assert any(h["session_id"] == SID for h in hits)


def test_import_refuses_tampered_archive(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"sessions/" + "a" * 24 + "/messages.ndjson": b"x"})
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)


def test_verify_rejects_degenerate_manifest(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"manifest.json": b"null"})
    result = archive.verify_archive(bad)
    assert result.ok is False
    assert "E_MANIFEST_INVALID" in {issue.code for issue in result.issues}
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(catalog.connect(tmp_path / "fresh.sqlite"), bad)


def test_build_archive_uses_lf_newlines(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    with zipfile.ZipFile(built.path) as zf:
        payloads = {name: zf.read(name) for name in zf.namelist()}
    assert "manifest.json" in payloads
    for name, data in payloads.items():
        assert b"\r\n" not in data, name


def test_import_archive_refreshes_content_hash(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    source_hash = conn.execute(
        "SELECT content_hash FROM sessions WHERE session_id=?", ("a" * 24,)
    ).fetchone()[0]
    assert source_hash
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    archive.import_archive(fresh, built.path)
    rows = fresh.execute("SELECT session_id, content_hash FROM sessions").fetchall()
    assert rows and all(row["content_hash"] for row in rows)
    imported = dict((row["session_id"], row["content_hash"]) for row in rows)
    assert imported["a" * 24] == source_hash


def test_verify_accepts_hostile_session_id_but_import_rejects(
    filled_db: Path, tmp_path: Path
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "hostile.zip"
    with zipfile.ZipFile(built.path) as zf:
        meta = json.loads(zf.read(META_NAME))
    meta["session_id"] = "../../../../escape"
    _rebuild(built.path, bad, mutate={META_NAME: json.dumps(meta).encode("utf-8")})
    assert archive.verify_archive(bad).ok is True

    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError) as exc:
        archive.import_archive(fresh, bad)
    assert "E_SESSION_ID" in {issue.code for issue in exc.value.issues}
    assert _running_runs(fresh) == []
    assert fresh.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert list(tmp_path.parent.glob("escape*")) == []
    assert list(tmp_path.parent.glob("*escape*")) == []


def test_verify_detects_missing_messages_ndjson(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rebuild(built.path, bad, drop={NDJSON_NAME})
    result = archive.verify_archive(bad)
    assert result.ok is False
    assert "E_SESSION_INCOMPLETE" in {issue.code for issue in result.issues}
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
    assert _running_runs(fresh) == []


def test_verify_detects_corrupt_ndjson_line(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rebuild(built.path, bad, mutate={NDJSON_NAME: b'{"message_id": "m1"}\nnot json\n'})
    result = archive.verify_archive(bad)
    assert result.ok is False
    assert "E_NDJSON_INVALID" in {issue.code for issue in result.issues}
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
    assert _running_runs(fresh) == []


def test_import_marks_run_failed_on_unexpected_error(
    filled_db: Path, tmp_path: Path
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rebuild(built.path, bad, mutate={NDJSON_NAME: b'{"seq": 1}\n'})
    assert archive.verify_archive(bad).ok is True
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
    rows = fresh.execute("SELECT result FROM import_runs").fetchall()
    assert rows and all(row["result"] == "failed" for row in rows)
    assert _running_runs(fresh) == []


def test_import_rejects_manifest_without_project(
    filled_db: Path, tmp_path: Path
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(built.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest.pop("project")
    _rebuild(built.path, bad,
             mutate={"manifest.json": json.dumps(manifest).encode("utf-8")})
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
    assert _running_runs(fresh) == []


def test_import_rejects_corrupt_meta_json(
    filled_db: Path, tmp_path: Path
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rebuild(built.path, bad, mutate={META_NAME: b"{not json"})
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
    assert _running_runs(fresh) == []
    assert fresh.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_build_failure_leaves_no_temp_file(
    filled_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = catalog.connect(filled_db)
    out_dir = tmp_path / "archives"
    monkeypatch.setattr(
        archive.verify, "verify_archive",
        lambda path: archive.VerifyResult(Path(path), False,
                                          (archive.VerifyIssue("E_STUB", "stub"),), None),
    )
    with pytest.raises(archive.ArchiveError):
        archive.build_archive(conn, out_dir, "-d-Work-RepoA")
    assert [p.name for p in out_dir.iterdir()] == []
    assert list(out_dir.glob(".tmp-*.zip")) == []


def test_zip_unreadable_detail_omits_absolute_path(
    filled_db: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")

    def boom(*args, **kwargs):
        error = OSError(2, "No such file or directory")
        error.filename = str(filled_db)
        raise error

    monkeypatch.setattr(archive.verify.zipfile, "ZipFile", boom)
    result = archive.verify_archive(built.path)
    codes = {issue.code: issue.detail for issue in result.issues}
    assert "E_ZIP_UNREADABLE" in codes
    assert str(filled_db).lower() not in codes["E_ZIP_UNREADABLE"].lower()
    assert "[Errno" not in codes["E_ZIP_UNREADABLE"]


def test_import_failed_detail_omits_absolute_path(
    filled_db: Path, tmp_path: Path, monkeypatch
) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")

    def boom(db_conn, record):
        raise sqlite3.OperationalError(
            f"unable to open database file: {filled_db}"
        )

    monkeypatch.setattr(catalog, "upsert_message", boom)
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError) as caught:
        archive.import_archive(fresh, built.path)
    codes = {issue.code: issue.detail for issue in caught.value.issues}
    assert "E_IMPORT_FAILED" in codes
    assert "<路径>" in codes["E_IMPORT_FAILED"]
    assert str(filled_db).lower() not in codes["E_IMPORT_FAILED"].lower()


def test_failure_text_keeps_plain_message(tmp_path: Path) -> None:
    from sessionpack.textutil import failure_text

    assert (
        failure_text(zipfile.BadZipFile("File is not a zip file"))
        == "File is not a zip file"
    )
    probe = tmp_path / "probe.sqlite"
    leaked = failure_text(ValueError(f"bad value at {probe}"))
    assert "<路径>" in leaked and str(probe).lower() not in leaked.lower()


def _add_share_message(conn) -> None:
    """给 SID 会话追加一条带分享图片引用的消息（模拟 share 导入产物）。"""
    from sessionpack.model import MessageRecord

    catalog.upsert_message(conn, MessageRecord(
        session_id=SID,
        message_id="b" * 24,
        seq=99,
        ts=None,
        intent=None,
        outcome=None,
        actions=(),
        learned=(),
        meta={"role": "user", "slug": "abc123", "images": [f"{SID}/x.png"]},
        body="看图",
        project_id="-d-Work-RepoA",
        source_kind="trae-export",
    ))
    conn.commit()


def test_archive_roundtrip_with_images(filled_db: Path, tmp_path: Path) -> None:
    from sessionpack.share_images import resolve_image

    conn = catalog.connect(filled_db)
    _add_share_message(conn)
    src_images = tmp_path / "src-images"
    (src_images / SID).mkdir(parents=True)
    (src_images / SID / "x.png").write_bytes(b"PNGDATA")
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA",
                                  images_dir=src_images)
    assert archive.verify_archive(built.path).ok is True
    with zipfile.ZipFile(built.path) as zf:
        assert f"sessions/{SID}/images/x.png" in zf.namelist()
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    dst_images = tmp_path / "dst-images"
    archive.import_archive(fresh, built.path, images_dir=dst_images)
    assert (dst_images / SID / "x.png").read_bytes() == b"PNGDATA"
    row = fresh.execute(
        "SELECT meta_json FROM messages WHERE message_id=?", ("b" * 24,)
    ).fetchone()
    assert json.loads(row["meta_json"])["images"] == [f"{SID}/x.png"]
    assert resolve_image(dst_images, f"{SID}/x.png") == (
        dst_images / SID / "x.png").resolve()


def test_build_archive_missing_image_and_id_changes(
    filled_db: Path, tmp_path: Path
) -> None:
    conn = catalog.connect(filled_db)
    _add_share_message(conn)
    empty = tmp_path / "empty-images"
    empty.mkdir()
    bare = archive.build_archive(conn, tmp_path / "a1", "-d-Work-RepoA",
                                 images_dir=empty)
    assert archive.verify_archive(bare.path).ok is True
    src_images = tmp_path / "src-images"
    (src_images / SID).mkdir(parents=True)
    (src_images / SID / "x.png").write_bytes(b"PNGDATA")
    withimg = archive.build_archive(conn, tmp_path / "a2", "-d-Work-RepoA",
                                    images_dir=src_images)
    assert withimg.archive_id != bare.archive_id


def test_resolve_image_rejects_hostile_paths(tmp_path: Path) -> None:
    from sessionpack.share_images import resolve_image

    root = tmp_path / "images"
    (root / SID).mkdir(parents=True)
    (root / SID / "x.png").write_bytes(b"1")
    assert resolve_image(root, f"{SID}/x.png") is not None
    assert resolve_image(root, "z" * 24 + "/x.png") is None  # 非法 session_id
    assert resolve_image(root, SID) is None  # 缺文件名段
    assert resolve_image(root, f"{SID}/sub/x.png") is None  # 段数超限
    assert resolve_image(root, f"{SID}/../x.png") is None  # 路径穿越
    assert resolve_image(root, f"{SID}/.hidden") is None  # 隐藏文件名
    assert resolve_image(root, f"{SID}/missing.png") is None  # 文件不存在


def test_link_share_images_copies_and_is_idempotent(tmp_path: Path) -> None:
    from sessionpack.model import MessageRecord
    from sessionpack.share_images import link_share_images

    db = tmp_path / "catalog.sqlite"
    conn = catalog.connect(db)
    catalog.init_schema(conn)
    catalog.upsert_message(conn, MessageRecord(
        session_id=SID, message_id="b" * 24, seq=1, ts=None, intent=None,
        outcome=None, actions=(), learned=(),
        meta={"role": "user", "slug": "abc123", "images": [f"{SID}/x.png"]},
        body="看图", project_id="trae-share", source_kind="trae-export"))
    conn.commit()
    conn.close()
    export_root = tmp_path / "export"
    (export_root / "images" / "abc123").mkdir(parents=True)
    (export_root / "images" / "abc123" / "x.png").write_bytes(b"DATA")
    dest = tmp_path / "images" / SID / "x.png"
    assert link_share_images(db, export_root) == 1
    assert dest.read_bytes() == b"DATA"
    # 幂等：大小相同则跳过
    assert link_share_images(db, export_root) == 1
    # 大小不一致则重新覆盖
    dest.write_bytes(b"tampered-longer")
    assert link_share_images(db, export_root) == 1
    assert dest.read_bytes() == b"DATA"
