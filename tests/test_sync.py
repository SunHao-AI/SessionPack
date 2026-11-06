import re
import shutil
from pathlib import Path

from sessionpack import archive, catalog, sync
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def _build_src_archives(memory_tree: Path, tmp_path: Path) -> tuple[Path, list[archive.ArchiveResult]]:
    """用 memory_tree 构建真实归档包，返回（归档目录, ArchiveResult 列表）。

    memory_tree 含 2 个项目 / 2 个会话 / 4 条消息（见 tests/conftest.py），
    因此返回 2 个 ArchiveResult。
    """
    db = tmp_path / "src-catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)   # 注意：第三参是 db 路径，不是 conn
    conn = catalog.connect(db)
    out = tmp_path / "src-archives"
    results = [archive.build_archive(conn, out, project["project_id"])
               for project in catalog.get_projects(conn)]
    conn.close()
    return out, results


def test_push_creates_dir_and_copies_all(memory_tree: Path, tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    result = sync.push_archives(src, share)
    assert share.is_dir()
    assert result.pushed == len(built)
    assert not result.errors
    assert {p.name for p in share.glob("*.zip")} == {b.path.name for b in built}
    assert result.bytes == sum(b.bytes for b in built)   # ArchiveResult.bytes 已由 cli._cmd_export 使用，字段名确认存在


def test_push_is_idempotent(memory_tree: Path, tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    sync.push_archives(src, share)
    again = sync.push_archives(src, share)
    assert again.pushed == 0
    assert again.skipped_existing == len(built)
    assert again.bytes == 0


def test_push_skips_bad_archive(memory_tree: Path, tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    (src / "broken-2026-11-06-0123456789ab.zip").write_bytes(b"not a zip")
    share = tmp_path / "share"
    result = sync.push_archives(src, share)
    assert result.pushed == len(built)
    assert len(result.skipped_unverified) == 1
    assert "broken-2026-11-06-0123456789ab.zip" in result.skipped_unverified[0]
    assert not (share / "broken-2026-11-06-0123456789ab.zip").exists()
    assert len(list(share.glob("*.zip"))) == len(built)


def test_push_dry_run_writes_nothing(memory_tree: Path, tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    result = sync.push_archives(src, share, dry_run=True)
    assert result.pushed == len(built)
    assert not share.exists()


def test_push_copy_failure_leaves_no_tmp(memory_tree: Path, tmp_path: Path,
                                         monkeypatch) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"

    def boom(file_src, file_dst, *args, **kwargs):
        raise OSError("复制失败 at C:\\Users\\secret\\x.zip")

    monkeypatch.setattr(shutil, "copy2", boom)
    result = sync.push_archives(src, share)
    assert result.pushed == 0
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)
    assert not list(share.glob(".tmp-*"))
