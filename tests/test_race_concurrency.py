"""Spec 9 R4：并发与竞态加固的回归测试。

覆盖探针实测复现的四类场景（F1 同名 push、F2 持库重建、F2b 并发重建、
F3 并发 import）与两处失败路径清理（config 残file、replace 良性消化）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sessionpack import archive, catalog, config, sync
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source
from sessionpack.paths import ensure_layout
from tests.conftest import write_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]


def _proc_env() -> dict[str, str]:
    return dict(os.environ, PYTHONIOENCODING="utf-8")


def _cli(*argv: str) -> subprocess.CompletedProcess[str]:
    """固定仓库根 cwd 的真实子进程入口，60s 超时必须失败而非挂死。"""
    return subprocess.run(
        [sys.executable, "-m", "sessionpack", *argv],
        cwd=str(REPO_ROOT), env=_proc_env(), capture_output=True,
        encoding="utf-8", timeout=90,
    )


def _real_package(tmp_path: Path, memory_tree: Path) -> Path:
    """用 memory_tree 构建一个真实归档包（push 会先 verify，假 zip 走不到复制）。"""
    db = tmp_path / "src-catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    conn = catalog.connect(db)
    try:
        first = catalog.get_projects(conn)[0]
        return archive.build_archive(conn, tmp_path / "src",
                                     first["project_id"]).path
    finally:
        conn.close()


# ---------- 1. 同名目标重叠写：唯一临时名使 copy 阶段不再互踩 ----------

def test_concurrent_push_same_dest_overlapping_copy(tmp_path: Path) -> None:
    """两个写者同时 _atomic_copy 同一目标：copy 阶段真实重叠、双双成功。

    R1 前临时名不含写者标识，A 的 finally 会删掉 B 在途的临时文件，
    B 的 replace 随即 FileNotFoundError；R1 后各自唯一，无需互让。
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"A" * (4 * 1024 * 1024))
    dest_dir = tmp_path / "dest"
    dest = dest_dir / "proj-2026-11-08-abcdef123456.zip"
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait(timeout=30)
            sync._atomic_copy(src, dest)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常交主线程断言
            failures.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert failures == []
    assert dest.read_bytes() == b"A" * (4 * 1024 * 1024)
    assert list(dest_dir.glob(".tmp-*")) == []


# ---------- 2. replace 撞共享冲突后的良性消化 ----------

def test_push_replace_race_benign_digestion(tmp_path: Path,
                                            memory_tree: Path,
                                            monkeypatch) -> None:
    """replace 撞 WinError 32：目标已被他人落定则消化，目标缺失则照报。

    同名包内容必同（文件名尾 12 位是内容 sha256），"已由他人落定"不是
    错误；但目标不存在说明是真实 IO 故障，必须进 errors 且不假装成功。
    """
    pkg = _real_package(tmp_path, memory_tree)
    src_dir = pkg.parent
    dest_dir = tmp_path / "share"
    dest_dir.mkdir()

    def landing_replace(file_src, file_dst):
        # 对手先一步把同名包放到位，再让本次 replace 撞共享冲突
        shutil.copy2(file_src, file_dst)
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(os, "replace", landing_replace)
    result = sync.push_archives(src_dir, dest_dir)
    assert result.errors == ()
    assert (dest_dir / pkg.name).read_bytes() == pkg.read_bytes()

    (dest_dir / pkg.name).unlink()

    def blocked_replace(file_src, file_dst):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(os, "replace", blocked_replace)
    result = sync.push_archives(src_dir, dest_dir)
    assert len(result.errors) == 1
    assert not (dest_dir / pkg.name).exists()
    assert list(dest_dir.glob(".tmp-*")) == []


# ---------- 3. config 写失败不留残file ----------

def test_set_sync_dir_replace_failure_leaves_no_tmp(
        tmp_path: Path, monkeypatch) -> None:
    """os.replace 失败：临时文件当场清掉，原 config 分毫未动，异常上抛。"""
    config.set_sync_dir(tmp_path, tmp_path / "share")
    before = (tmp_path / "config.toml").read_bytes()

    def boom(file_src, file_dst):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(PermissionError):
        config.set_sync_dir(tmp_path, tmp_path / "other")
    assert not (tmp_path / "config.toml.tmp").exists()
    assert (tmp_path / "config.toml").read_bytes() == before


# ---------- 4. 他进程持库时：重建优雅失败且现场干净 ----------

def _rebuild_tree(tmp_path: Path) -> Path:
    """铺一个可重建的数据目录：import → export --archive，返回 data_dir。"""
    tree = tmp_path / "memory"
    write_jsonl(
        tree / "projects" / "-d-Work-RepoA" / "20260101"
        / f"session_memory_{'a' * 24}.jsonl",
        [{"message_id": "m1", "message_summary_time": "2026-01-01 09:00:00",
          "intent": "竞态", "outcome": "持库时重建", "actions": [],
          "learned": []}])
    data_dir = tmp_path / "data"
    imp = _cli("import", "--memory-root", str(tree),
               "--data-dir", str(data_dir))
    assert imp.returncode == 0, imp.stderr
    # project_id 以 - 开头，必须用 --project= 形式，否则 argparse 误判为选项
    exp = _cli("export", "--project=-d-Work-RepoA", "--archive",
               "--out", str(data_dir / "archives"),
               "--data-dir", str(data_dir))
    assert exp.returncode == 0, exp.stderr
    return data_dir


def _holder(*paths: str) -> subprocess.Popen[str]:
    """持库子进程：对每个库开连接推进事务提交，随后存活 25 秒。"""
    script = (
        "import sqlite3, time\n"
        f"for p in {list(paths)!r}:\n"
        "    c = sqlite3.connect(p, timeout=10)\n"
        "    c.execute('UPDATE sessions SET title=title')\n"
        "    c.commit()\n"
        "time.sleep(25)\n"
    )
    return subprocess.Popen([sys.executable, "-c", script], env=_proc_env())


def test_rebuild_from_archives_while_held_fails_cleanly(
        tmp_path: Path) -> None:
    """他进程持有原库时重建：重试耗尽后退 1、原库完好、无残留。"""
    data_dir = _rebuild_tree(tmp_path)
    layout = ensure_layout(data_dir)
    holder = _holder(str(layout.catalog))
    try:
        time.sleep(2.0)  # 等 holder 真正开上连接并提交
        res = _cli("rebuild", "--from-archives", "--yes", "--json",
                   "--data-dir", str(data_dir))
    finally:
        holder.terminate()
        holder.wait(timeout=15)

    assert res.returncode == 1
    # 失败路径 data 为 None：--json 下错误行走 stderr，stdout 无 JSON
    assert "原索引未改动" in res.stderr
    conn = catalog.connect_readonly(layout.catalog)
    try:
        assert catalog.stats(conn)["sessions"] == 1
    finally:
        conn.close()
    assert list(data_dir.glob("catalog.sqlite.staging*")) == []


def test_concurrent_rebuild_from_archives_invariants(tmp_path: Path) -> None:
    """三进程并发全量重建：赢家成功、败者（若有）干净失败且不留脏现场。

    staging 已唯一化（spec9 实施补），预期互踩消失；断言仍按不变量：
    至少一个成功；败者的错误必须声明原索引未改动；无 staging 残留；
    .bak 单份不爆炸；最终索引精确可查。
    """
    data_dir = _rebuild_tree(tmp_path)
    procs = [subprocess.Popen(
        [sys.executable, "-m", "sessionpack", "rebuild", "--from-archives",
         "--yes", "--json", "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT), env=_proc_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8") for _ in range(3)]
    outs = []
    for p in procs:
        try:
            outs.append(p.communicate(timeout=90))
        except subprocess.TimeoutExpired:
            p.kill()
            raise

    codes = [p.returncode for p in procs]
    assert codes.count(0) >= 1
    for code, (out, err) in zip(codes, outs, strict=True):
        if code == 0:
            assert json.loads(out)["ok"] is True
        else:
            assert "原索引未改动" in err, (out, err)
    layout = ensure_layout(data_dir)
    assert list(data_dir.glob("catalog.sqlite.staging*")) == []
    assert len(list(data_dir.glob("catalog.sqlite.bak*"))) <= 1
    conn = catalog.connect_readonly(layout.catalog)
    try:
        stats = catalog.stats(conn)
    finally:
        conn.close()
    assert (stats["sessions"], stats["messages"], stats["fts_rows"]) \
        == (1, 1, 1)


# ---------- 5. 多进程并发 import：SQLite 层安全性锁死 ----------

def test_concurrent_import_converges_exactly(tmp_path: Path) -> None:
    """四进程同时 import 同一库：全退 0、stats 精确、重跑幂等不翻倍。"""
    tree = tmp_path / "memory"
    for sid, day in (("a" * 24, "20260101"), ("b" * 24, "20260102")):
        write_jsonl(
            tree / "projects" / "-d-Work-RepoA" / day
            / f"session_memory_{sid}.jsonl",
            [{"message_id": f"{sid[:4]}m1",
              "message_summary_time": "2026-01-01 09:00:00",
              "intent": "并发", "outcome": "同库导入", "actions": [],
              "learned": []}])
    data_dir = tmp_path / "data"
    procs = [subprocess.Popen(
        [sys.executable, "-m", "sessionpack", "import",
         "--memory-root", str(tree), "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT), env=_proc_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8") for _ in range(4)]
    for p in procs:
        try:
            _, err = p.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            p.kill()
            raise
        assert p.returncode == 0, err

    def _stats() -> dict:
        conn = catalog.connect_readonly(data_dir / "catalog.sqlite")
        try:
            s = catalog.stats(conn)
        finally:
            conn.close()
        return {k: s[k] for k in ("sessions", "messages", "fts_rows")}

    first = _stats()
    assert first == {"sessions": 2, "messages": 2, "fts_rows": 2}
    again = _cli("import", "--memory-root", str(tree),
                 "--data-dir", str(data_dir))
    assert again.returncode == 0
    assert _stats() == first
