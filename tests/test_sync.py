import re
import shutil
from pathlib import Path

from sessionpack import archive, catalog, export, sync
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.cli import main
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


def _fresh_conn(tmp_path: Path):
    conn = catalog.connect(tmp_path / "dst-catalog.sqlite")
    catalog.init_schema(conn)
    return conn


def _push_all(memory_tree: Path, tmp_path: Path) -> Path:
    src, _ = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    sync.push_archives(src, share)
    return share


def test_pull_copies_and_imports(memory_tree: Path, tmp_path: Path) -> None:
    share = _push_all(memory_tree, tmp_path)
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    result = sync.pull_archives(dst, share, conn)
    assert result.pulled == len(list(share.glob("*.zip")))
    assert not result.errors
    assert {p.name for p in dst.glob("*.zip")} == {p.name for p in share.glob("*.zip")}
    assert result.sessions_added == 2
    assert result.messages_added == 4
    stats = catalog.stats(conn)
    assert stats["messages"] == 4
    conn.close()


def test_pull_repeated_adds_nothing(memory_tree: Path, tmp_path: Path) -> None:
    share = _push_all(memory_tree, tmp_path)
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    sync.pull_archives(dst, share, conn)
    again = sync.pull_archives(dst, share, conn)
    assert again.pulled == 0
    assert again.skipped_existing == len(list(share.glob("*.zip")))
    assert again.sessions_added == 0
    assert again.messages_added == 0
    conn.close()


def test_pull_skips_invalid_without_touching_local(memory_tree: Path,
                                                   tmp_path: Path) -> None:
    share = tmp_path / "share"          # 独立同步目录：只放一个坏包
    share.mkdir()
    bad = share / "broken-2026-11-06-0123456789ab.zip"
    bad.write_bytes(b"not a zip")
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    before = catalog.stats(conn)
    result = sync.pull_archives(dst, share, conn)
    assert not (dst / bad.name).exists()
    assert result.pulled == 0
    assert len(result.skipped_invalid) == 1
    assert bad.name in result.skipped_invalid[0]
    assert catalog.stats(conn) == before
    conn.close()


def test_pull_dedups_same_id_different_filename(memory_tree: Path,
                                                tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    sync.push_archives(src, share)
    original = sorted(share.glob("*.zip"))[0]
    # 构造同 ID（尾 12 位 hex 相同）、日期段不同的第二份文件名，模拟跨日重名
    renamed = share / re.sub(r"-\d{4}-\d{2}-\d{2}-", "-2027-01-02-", original.name)
    assert renamed.name != original.name
    shutil.copy2(original, renamed)
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    first = sync.pull_archives(dst, share, conn)
    total = len(list(share.glob("*.zip")))
    assert first.pulled == total - 1        # 同 ID 的两份只导入一次
    assert first.skipped_existing == 1
    again = sync.pull_archives(dst, share, conn)
    assert again.pulled == 0
    conn.close()


def test_pull_error_text_sanitized(memory_tree: Path, tmp_path: Path,
                                   monkeypatch) -> None:
    share = _push_all(memory_tree, tmp_path)
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)

    def boom(connection, path):
        raise OSError("导入失败 at C:\\Users\\secret\\x.zip")

    monkeypatch.setattr(archive, "import_archive", boom)
    result = sync.pull_archives(dst, share, conn)
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)
    conn.close()


def test_two_device_sync_roundtrip(memory_tree: Path, tmp_path: Path) -> None:
    """规格验收口径：两个 data-root + 一个共享目录完成 push → pull → 检索 → 导出一致。"""
    dev_a = tmp_path / "device-a"
    dev_b = tmp_path / "device-b"
    share = tmp_path / "share"

    # 设备 A：导入 + 生成归档包 + 推送
    assert main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(dev_a)]) == 0
    assert main(["export", "--data-dir", str(dev_a), "--all", "--archive",
                 "--out", str(dev_a / "archives")]) == 0
    assert main(["push", "--dir", str(share), "--data-dir", str(dev_a)]) == 0

    # 设备 B：拉取（自动复制 + 导入）
    assert main(["pull", "--dir", str(share), "--data-dir", str(dev_b)]) == 0

    conn_a = catalog.connect(dev_a / "catalog.sqlite")
    conn_b = catalog.connect(dev_b / "catalog.sqlite")
    try:
        project = catalog.get_projects(conn_a)[0]
        # B 可检索：与 test_server.py 同口径，用 memory_tree 中已知关键词 "sha256"
        assert catalog.search_messages(conn_b, "sha256")
        assert catalog.stats(conn_b)["messages"] == catalog.stats(conn_a)["messages"]
        # 导出与 A 逐字节一致
        exp_a = export.export_project(conn_a, project["project_id"], tmp_path / "exp-a")
        exp_b = export.export_project(conn_b, project["project_id"], tmp_path / "exp-b")
        assert [p.name for p in exp_a.paths] == [p.name for p in exp_b.paths]
        for left, right in zip(exp_a.paths, exp_b.paths):
            assert left.read_bytes() == right.read_bytes()
    finally:
        conn_a.close()
        conn_b.close()
