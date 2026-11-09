import re
import shutil
from pathlib import Path

import pytest

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


def test_push_stat_failure_is_sanitized(memory_tree: Path, tmp_path: Path,
                                        monkeypatch) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"

    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self.suffix == ".zip":
            raise OSError("boom C:\\Users\\secret")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    result = sync.push_archives(src, share)
    assert result.pushed == 0
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)


def test_push_dry_run_stat_failure_is_sanitized(memory_tree: Path,
                                                tmp_path: Path,
                                                monkeypatch) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"

    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self.suffix == ".zip":
            raise OSError("boom C:\\Users\\secret")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    result = sync.push_archives(src, share, dry_run=True)
    assert result.pushed == 0
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)


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


def test_push_skips_temp_residue(memory_tree: Path, tmp_path: Path) -> None:
    src, built = _build_src_archives(memory_tree, tmp_path)
    (src / ".tmp-good-2026-11-06-abcdef012345.zip").write_bytes(b"residue")
    (src / ".build-x.zip").write_bytes(b"residue")
    share = tmp_path / "share"
    result = sync.push_archives(src, share)
    assert result.pushed == len(built)
    assert not result.errors
    assert not result.skipped_unverified
    assert not list(share.glob(".*"))


def test_pull_skips_temp_residue_and_missing_dir(memory_tree: Path,
                                                 tmp_path: Path) -> None:
    share = _push_all(memory_tree, tmp_path)
    (share / ".tmp-leftover-2026-11-06-abcdef012345.zip").write_bytes(b"residue")
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    result = sync.pull_archives(dst, share, conn)
    assert not result.errors
    assert not result.skipped_invalid
    assert not list(dst.glob(".*"))
    conn.close()
    (tmp_path / "empty").mkdir()
    conn2 = _fresh_conn(tmp_path / "empty")
    missing = sync.pull_archives(dst, tmp_path / "no-such-dir", conn2)
    assert missing.pulled == 0
    assert not missing.errors
    conn2.close()


def test_pull_temp_residue_in_local_not_dedup_key(memory_tree: Path,
                                                  tmp_path: Path) -> None:
    share = _push_all(memory_tree, tmp_path)
    dst = tmp_path / "dst-archives"
    dst.mkdir(parents=True)
    first = sorted(p.name for p in share.glob("*.zip"))[0]
    (dst / (".tmp-" + first)).write_bytes(b"")
    conn = _fresh_conn(tmp_path)
    result = sync.pull_archives(dst, share, conn)
    assert result.pulled == len(list(share.glob("*.zip")))
    assert not result.errors
    conn.close()


def test_pull_renames_stable_by_filename(memory_tree: Path,
                                         tmp_path: Path) -> None:
    """无 id12 尾巴的包（手工改名）：首轮复制导入，二轮按文件名判同跳过。"""
    share = tmp_path / "share"
    share.mkdir()
    src, built = _build_src_archives(memory_tree, tmp_path)
    for p in src.glob("*.zip"):
        shutil.copy2(p, share / "hand.zip")
        break
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)
    first = sync.pull_archives(dst, share, conn)
    assert first.pulled == 1
    assert not first.errors
    again = sync.pull_archives(dst, share, conn)
    assert again.pulled == 0
    assert again.skipped_existing == 1
    conn.close()


def test_pull_copy_failure_leaves_no_tmp(memory_tree: Path, tmp_path: Path,
                                         monkeypatch) -> None:
    share = _push_all(memory_tree, tmp_path)
    dst = tmp_path / "dst-archives"
    conn = _fresh_conn(tmp_path)

    def boom(file_src, file_dst, *args, **kwargs):
        raise OSError("复制失败 at C:\\Users\\secret\\x.zip")

    monkeypatch.setattr(shutil, "copy2", boom)
    result = sync.pull_archives(dst, share, conn)
    assert result.pulled == 0
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)
    assert not list(dst.glob(".tmp-*"))
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
        for left, right in zip(exp_a.paths, exp_b.paths, strict=True):
            assert left.read_bytes() == right.read_bytes()
    finally:
        conn_a.close()
        conn_b.close()


# ---------------- 加密推送（Spec 14 D3） ----------------

PW = "正确的马battery"


def test_push_encrypt_writes_envelopes_only(memory_tree, tmp_path):
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    result = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert not result.errors
    assert result.pushed == len(built)
    envelopes = sorted(share.glob("*.spk"))
    assert len(envelopes) == len(built)
    assert all(n.startswith("spk-") for n in [p.name for p in envelopes])
    assert list(share.glob("*.zip")) == []          # 密文模式不落明文
    assert result.bytes > 0                          # 报告字节沿用明文包大小口径


def test_push_encrypt_dedup_by_archive_id(memory_tree, tmp_path):
    """信封名是随机的，判重只能靠解出的 archive_id。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    sync.push_archives(src, share, encrypt=True, passphrase=PW)
    first_names = {p.name for p in share.glob("*.spk")}
    again = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert again.pushed == 0
    assert again.skipped_existing == len(built)
    assert again.bytes == 0
    assert {p.name for p in share.glob("*.spk")} == first_names   # 未新增文件


def test_push_encrypt_rejects_missing_passphrase(memory_tree, tmp_path):
    src, _ = _build_src_archives(memory_tree, tmp_path)
    with pytest.raises(ValueError) as exc:
        sync.push_archives(src, tmp_path / "share", encrypt=True)
    assert "E201" in str(exc.value)


def test_push_encrypt_skips_unverified_without_reading_id(memory_tree, tmp_path):
    """加密模式同样先 verify：坏包不读 id、不落盘。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    (src / "broken-2026-11-06-0123456789ab.zip").write_bytes(b"not a zip")
    share = tmp_path / "share"
    result = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert result.pushed == len(built)
    assert len(result.skipped_unverified) == 1
    assert "broken-2026-11-06-0123456789ab.zip" in result.skipped_unverified[0]
    assert len(list(share.glob("*.spk"))) == len(built)


def test_push_encrypt_leaves_no_tmp_on_failure(memory_tree, tmp_path, monkeypatch):
    src, _ = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"

    def boom(*args, **kwargs):
        raise OSError("写入失败 at C:\\Users\\secret\\x.spk")

    monkeypatch.setattr(Path, "write_bytes", boom)
    result = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert result.pushed == 0
    assert result.errors
    assert "C:\\Users" not in " ".join(result.errors)
    assert "<路径>" in " ".join(result.errors)
    assert [p.name for p in share.iterdir()] == []


def test_push_encrypt_dry_run_writes_nothing(memory_tree, tmp_path):
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    result = sync.push_archives(src, share, dry_run=True,
                                encrypt=True, passphrase=PW)
    assert result.pushed == len(built)
    assert not share.exists()


def test_push_ignores_foreign_garbage_when_matching(memory_tree, tmp_path):
    """同步目录里的坏信封不参与判重，也不被 push 改动。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    share.mkdir(parents=True)
    (share / ("spk-" + "f" * 32 + ".spk")).write_bytes(b"garbage")
    result = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert result.pushed == len(built)
    assert not result.errors
    assert (share / ("spk-" + "f" * 32 + ".spk")).read_bytes() == b"garbage"


def test_push_encrypt_counts_undecryptable_envelopes(memory_tree, tmp_path):
    """B1：无法解密的信封计入 skipped_undecryptable，重推行为不变。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    share.mkdir(parents=True)
    # 两个无法解密的规范名信封：纯垃圾 + 口令不符（他人口令加密）
    (share / ("spk-" + "f" * 32 + ".spk")).write_bytes(b"garbage")
    sync.push_archives(src, tmp_path / "other", encrypt=True,
                       passphrase="另一个口令")
    foreign = sorted((tmp_path / "other").glob("*.spk"))[0]
    shutil.copy2(foreign, share / foreign.name)
    result = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert result.pushed == len(built)               # 仍按本地包重推，行为不变
    assert result.skipped_existing == 0
    assert result.skipped_undecryptable == 2
    assert not result.errors


def test_push_encrypt_undecryptable_zero_when_all_readable(memory_tree, tmp_path):
    """可解密信封场景：计数保持 0，不误报。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    first = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert first.skipped_undecryptable == 0
    again = sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert again.pushed == 0
    assert again.skipped_existing == len(built)
    assert again.skipped_undecryptable == 0


def test_push_plain_mode_undecryptable_stays_zero(memory_tree, tmp_path):
    """非加密 push 不做信封判重：即使目录有垃圾信封计数仍为 0。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    share.mkdir(parents=True)
    (share / ("spk-" + "f" * 32 + ".spk")).write_bytes(b"garbage")
    result = sync.push_archives(src, share)
    assert result.pushed == len(built)
    assert result.skipped_undecryptable == 0


# ---------------- 加密拉取（Spec 14 D3/D6） ----------------

def _pull_target(tmp_path):
    """返回（本机 archives 目录路径, 全新索引 conn）。"""
    return tmp_path / "dst-archives", _fresh_conn(tmp_path)


def _encrypt_share(memory_tree, tmp_path):
    """加密推送一整套包，返回 (src, share, built)。"""
    src, built = _build_src_archives(memory_tree, tmp_path)
    share = tmp_path / "share"
    sync.push_archives(src, share, encrypt=True, passphrase=PW)
    assert len(list(share.glob("*.spk"))) == len(built)
    return src, share, built


def test_pull_encrypt_roundtrip(memory_tree, tmp_path):
    src, share, built = _encrypt_share(memory_tree, tmp_path)
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert not result.errors
    assert result.pulled == len(built)
    # 信封名与内容无关，落本机必须重建规范包名（与 src 构建出的名字一致）
    assert {p.name for p in dst.glob("*.zip")} == {b.path.name for b in built}
    assert result.sessions_added == 2
    assert result.messages_added == 4
    assert catalog.stats(conn)["messages"] == 4
    assert not list(dst.glob("*.spk"))       # 本机从不落信封文件
    conn.close()


def test_pull_encrypt_rejects_missing_passphrase(memory_tree, tmp_path):
    _, share, _ = _encrypt_share(memory_tree, tmp_path)
    dst, conn = _pull_target(tmp_path)
    with pytest.raises(ValueError) as exc:
        sync.pull_archives(dst, share, conn, encrypt=True)
    assert "E201" in str(exc.value)
    conn.close()


def test_pull_encrypt_wrong_passphrase_skips_all(memory_tree, tmp_path):
    """口令错：全部信封入 skipped_invalid，本机一个文件都不落（spec §6）。"""
    _, share, built = _encrypt_share(memory_tree, tmp_path)
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True,
                                passphrase="错的口令")
    assert result.pulled == 0
    assert len(result.skipped_invalid) == len(built)
    assert all("口令错误或文件损坏" in item for item in result.skipped_invalid)
    assert not dst.exists() or list(dst.iterdir()) == []
    conn.close()


def test_pull_encrypt_tampered_envelope_skipped(memory_tree, tmp_path):
    """篡改密文字节 → GCM 认证失败，跳过且不落本机文件。"""
    _, share, built = _encrypt_share(memory_tree, tmp_path)
    env = sorted(share.glob("*.spk"))[0]
    blob = bytearray(env.read_bytes())
    blob[-1] ^= 0x01
    env.write_bytes(bytes(blob))
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert result.pulled == len(built) - 1   # 其余信封照常导入
    assert len(result.skipped_invalid) == 1
    assert env.name in result.skipped_invalid[0]
    assert "口令错误或文件损坏" in result.skipped_invalid[0]
    conn.close()


def test_pull_encrypt_renamed_envelope_skipped(memory_tree, tmp_path):
    """AAD 绑定文件名（D5）：云侧改名后认证必失败，坏信封不伤其余。"""
    _, share, built = _encrypt_share(memory_tree, tmp_path)
    env = sorted(share.glob("*.spk"))[0]
    env.rename(share / ("spk-" + "a" * 32 + ".spk"))
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert result.pulled == len(built) - 1
    assert len(result.skipped_invalid) == 1
    assert "spk-" + "a" * 32 + ".spk" in result.skipped_invalid[0]
    conn.close()


def test_pull_encrypt_idempotent(memory_tree, tmp_path):
    """再拉一次：规范包名的尾 12 位命中本机 id 集合，全部跳过。"""
    _, share, built = _encrypt_share(memory_tree, tmp_path)
    dst, conn = _pull_target(tmp_path)
    sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    again = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert again.pulled == 0
    assert again.skipped_existing == len(built)
    assert again.sessions_added == 0
    assert again.messages_added == 0
    conn.close()


def test_pull_encrypt_mixed_zip_spk_first_wins(memory_tree, tmp_path):
    """同 archive_id 的明文与信封并存（迁移中间态）：先导入者赢，另一个跳过。"""
    src, share, built = _encrypt_share(memory_tree, tmp_path)
    sync.push_archives(src, share)           # 明文也放进去
    suffixes = [p.suffix for p in sync._scan_sync_candidates(share)]
    assert suffixes == [".zip"] * len(built) + [".spk"] * len(built)
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert result.pulled == len(built)               # zip 在候选中排先，先导
    assert result.skipped_existing == len(built)     # 信封同 id 全判已存在
    conn.close()


def test_pull_plain_mode_ignores_envelopes(memory_tree, tmp_path):
    """不开加密时 .spk 视为不可见（不开加密零行为变更，spec 目标 5）。"""
    _, share, _ = _encrypt_share(memory_tree, tmp_path)
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn)
    assert result.pulled == 0
    assert result.skipped_existing == 0
    assert not result.skipped_invalid
    assert not result.errors
    conn.close()


def test_pull_encrypt_read_failure_goes_to_errors(memory_tree, tmp_path,
                                                 monkeypatch):
    """信封读不动（IO 故障）：入 errors 且脱敏，不中止整轮，其余照常导入。"""
    _, share, built = _encrypt_share(memory_tree, tmp_path)
    broken = sorted(share.glob("*.spk"))[0]
    real_read = Path.read_bytes

    def boom(self):
        if self.name == broken.name:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    dst, conn = _pull_target(tmp_path)
    result = sync.pull_archives(dst, share, conn, encrypt=True, passphrase=PW)
    assert result.pulled == len(built) - 1
    assert not result.skipped_invalid
    assert len(result.errors) == 1
    assert broken.name in result.errors[0]
    joined = " ".join(result.errors)
    assert str(tmp_path) not in joined and "C:\\Users" not in joined
    assert len(list(dst.glob("*.zip"))) == len(built) - 1
    conn.close()


def test_two_device_encrypted_sync_roundtrip(memory_tree: Path, tmp_path: Path,
                                             monkeypatch, capsys) -> None:
    """双设备经同一加密同步目录互通有无（spec §7 端到端项）：
    A 推信封、B pull 解密导入；B 再推走同一信封链路；口令错的一方全跳过。"""
    passphrase = "s3cret-pass"                    # A/B 共用；C 故意用错口令
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", passphrase)
    # 设备 A：import → export --all --archive（包落 data_dir/archives/，§2.2）
    data_a = tmp_path / "data-a"
    assert main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_a)]) == 0
    assert main(["export", "--data-dir", str(data_a), "--all", "--archive",
                 "--out", str(data_a / "archives")]) == 0
    assert len(list((data_a / "archives").glob("*.zip"))) == 2   # memory_tree 两个项目
    # 共享同步目录：A 加密推送（--dir 现场指定并持久化）
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share), "--encrypt",
                 "--data-dir", str(data_a)]) == 0
    assert len(list(share.glob("spk-*.spk"))) == 2
    # 云侧视角：目录里只有信封，明文包一个都不留
    assert not list(share.glob("*.zip"))
    # 设备 B：pull 按后缀自适应解密导入（口令来自环境变量，零配置键参与，D7）
    data_b = tmp_path / "data-b"
    assert main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_b)]) == 0
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_b)]) == 0
    assert len(list((data_b / "archives").glob("*.zip"))) == 2
    assert not list((data_b / "archives").glob("*.spk"))
    # B 可检索：与 test_two_device_sync_roundtrip 同口径，用 memory_tree 已知关键词
    conn_b = catalog.connect(data_b / "catalog.sqlite")
    try:
        assert catalog.search_messages(conn_b, "sha256")
    finally:
        conn_b.close()
    # B 再推：与 A 同名同 archive_id 的包按 id 判重全跳过，仍走 --encrypt 链路
    assert main(["push", "--dir", str(share), "--encrypt",
                 "--data-dir", str(data_b)]) == 0
    assert len(list(share.glob("spk-*.spk"))) == 2
    # 设备 A 回拉：同 id 判重跳过，口令一致不报错
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_a)]) == 0
    assert "已拉取 0 个" in capsys.readouterr().out
    # 口令错的设备 C：信封全部进 skipped_invalid，本机 archives 零落盘
    data_c = tmp_path / "data-c"
    assert main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_c)]) == 0
    for p in (data_c / "archives").glob("*.zip"):
        p.unlink()
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "wrong-pass")
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_c)]) == 0
    assert list((data_c / "archives").iterdir()) == []
