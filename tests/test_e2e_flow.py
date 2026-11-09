"""Spec 8 E2E-1：双设备全流程贯穿（import→检索→归档→push→pull→rebuild→serve）。

与碎片闭环不同，本文件的一条测试串起全部环节：任何一环断裂当场暴露。
步骤断言全部带步骤号，红行自解释。
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sessionpack import catalog
from sessionpack.cli.read_cmds import run_doctor
from sessionpack.cli.write_cmds import (
    run_export,
    run_import,
    run_pull,
    run_push,
    run_rebuild,
)
from sessionpack.server import create_server
from tests.conftest import SID_A, SID_B


def _ns(**kwargs) -> argparse.Namespace:
    """构造与 parser 等价的 args 对象（与 test_cli_commands 同法，测试间不互导）。"""
    base = dict(memory_root=None, data_dir=None, json=False, encrypt=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _sync_to_b(memory_tree: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """主链步骤 1-5：A 导入→导出归档→push 到 sync，B pull 入库。

    返回 (A 数据目录, B 数据目录, 同步目录)。
    """
    data_a = tmp_path / "device-a"
    data_b = tmp_path / "device-b"
    sync = tmp_path / "sync"
    sync.mkdir()

    # 步骤 1：A 导入（2 会话 4 消息，坏行 2：坏 topics 行 + 坏 jsonl 行）
    r1 = run_import(_ns(memory_root=str(memory_tree), data_dir=str(data_a),
                        json=True, archive=None, project=None, since=None,
                        until=None, reparse=False, retry_failed=False,
                        audit=False))
    assert r1.exit_code == 0, f"步骤1 导入失败: {r1.err_lines}"
    assert r1.data["sessions_created"] == 2, "步骤1 会话数应为 2"
    assert r1.data["messages_inserted"] == 4, "步骤1 消息数应为 4"
    assert r1.data["rows_failed"] == 2, "步骤1 坏行应被记录而不中断"

    # 步骤 3：A 导出全部项目为归档包，落 A/archives
    r3 = run_export(_ns(data_dir=str(data_a), out=str(data_a / "archives"),
                        archive=True, all=True, session=None, project=None,
                        format="markdown"))
    assert r3.exit_code == 0, f"步骤3 导出失败: {r3.err_lines}"
    zips_a = sorted((data_a / "archives").glob("*.zip"))
    assert len(zips_a) == 2, "步骤3 应导出 2 个项目包"

    # 步骤 4：A push 到共享同步目录
    r4 = run_push(_ns(data_dir=str(data_a), dir=str(sync), dry_run=False,
                      json=True))
    assert r4.exit_code == 0, f"步骤4 push 失败: {r4.err_lines}"
    assert r4.data["pushed"] == 2, "步骤4 应推送 2 个包"
    assert len(list(sync.glob("*.zip"))) == 2, "步骤4 sync 目录应可见 2 个包"

    # 步骤 5：B pull（包复制到 B/archives 并入库）
    r5 = run_pull(_ns(data_dir=str(data_b), dir=str(sync), json=True))
    assert r5.exit_code == 0, f"步骤5 pull 失败: {r5.err_lines}"
    assert r5.data["pulled"] == 2, "步骤5 应拉取 2 个包"
    assert r5.data["sessions_added"] == 2, "步骤5 B 应新增 2 会话"
    assert r5.data["messages_added"] == 4, "步骤5 B 应新增 4 消息"
    assert len(list((data_b / "archives").glob("*.zip"))) == 2, \
        "步骤5 B/archives 应落 2 个包"
    return data_a, data_b, sync


def test_e2e_full_flow_two_devices(memory_tree: Path, tmp_path: Path) -> None:
    """E2E-1 主链：步骤 1-9 单条贯通（步骤 10 serve 环独立成测）。"""
    # 步骤 1-5：A→sync→B（见 _sync_to_b）
    data_a, data_b, _sync = _sync_to_b(memory_tree, tmp_path)

    # 步骤 2：A 侧检索环——"校验"命中 RepoA 会话的 m2
    conn_a = catalog.connect(data_a / "catalog.sqlite")
    try:
        hits = catalog.search_messages(conn_a, "校验")
        assert [(r["session_id"], r["message_id"]) for r in hits] == \
            [(SID_A, "m2")], "步骤2 A 侧检索应命中 RepoA m2"

        # 步骤 6：B 侧跨机检索——"校验归档"经 FTS trigram 命中 pull 而来的会话
        conn_b = catalog.connect(data_b / "catalog.sqlite")
        try:
            hits_b = catalog.search_messages(conn_b, "校验归档")
            assert [(r["session_id"], r["message_id"]) for r in hits_b] == \
                [(SID_A, "m2")], "步骤6 B 侧应命中来自 A 的会话"
        finally:
            conn_b.close()
    finally:
        conn_a.close()

    # 步骤 7：手删 1 条 message_fts 行 → doctor 判漂移，检索当场失效
    conn_b = catalog.connect(data_b / "catalog.sqlite")
    try:
        conn_b.execute(
            "DELETE FROM message_fts WHERE session_id=? AND message_id=?",
            (SID_A, "m2"))
        conn_b.commit()
        assert catalog.search_messages(conn_b, "校验归档") == [], \
            "步骤7 FTS 行被删后检索应落空（漂移可感知）"
    finally:
        conn_b.close()
    r7 = run_doctor(_ns(memory_root=str(memory_tree), data_dir=str(data_b),
                        json=True))
    assert r7.exit_code == 0, "步骤7 doctor 记忆目录存在应退 0"
    fts7 = r7.data["catalog"]["fts"]
    assert fts7["drift"] is True and fts7["fts_missing"] == 1, \
        f"步骤7 doctor 应报漂移: {fts7}"

    # 步骤 8：rebuild --fts 修好，检索恢复
    r8 = run_rebuild(_ns(data_dir=str(data_b), fts=True, from_archives=False,
                         yes=False, json=True))
    assert r8.exit_code == 0, f"步骤8 rebuild --fts 失败: {r8.err_lines}"
    assert r8.data["rebuilt"] == 4, "步骤8 应重建 4 条 FTS"
    assert r8.data["fts"]["drift"] is False, "步骤8 重建后应无漂移"
    conn_b = catalog.connect(data_b / "catalog.sqlite")
    try:
        assert catalog.search_messages(conn_b, "校验归档"), \
            "步骤8 重建后步骤 6 的检索应恢复"
    finally:
        conn_b.close()

    # 步骤 9：从归档包全量重建，stats 与 A 一致、.bak 在、无 .staging 残留
    conn_a = catalog.connect(data_a / "catalog.sqlite")
    try:
        stats_a = catalog.stats(conn_a)
    finally:
        conn_a.close()
    r9 = run_rebuild(_ns(data_dir=str(data_b), fts=False, from_archives=True,
                         yes=True, json=True))
    assert r9.exit_code == 0, f"步骤9 全量重建失败: {r9.data['error']}"
    assert r9.data["ok"] is True
    assert (r9.data["sessions"], r9.data["messages"], r9.data["fts_rows"]) \
        == (stats_a["sessions"], stats_a["messages"], stats_a["fts_rows"]), \
        "步骤9 B 重建后统计应与 A 一致"
    assert Path(r9.data["backup"]).is_file(), "步骤9 换入前旧库应备份为 .bak"
    assert (data_b / "catalog.sqlite.bak").is_file()
    leftovers = [p for p in data_b.rglob("*") if ".staging" in p.name]
    assert leftovers == [], f"步骤9 不应残留 staging: {leftovers}"
    conn_b = catalog.connect(data_b / "catalog.sqlite")
    try:
        assert catalog.stats(conn_b) == stats_a, "步骤9 B 最终 stats 应与 A 全等"
    finally:
        conn_b.close()


def test_e2e_serve_after_pull(memory_tree: Path, tmp_path: Path) -> None:
    """E2E-1 步骤 10：pull 而来的 B 库可直接 serve（HTTP API 环）。

    独立成测：需要起线程，与主链纯函数断言分离，失败定位面更小。
    """
    _data_a, data_b, _sync = _sync_to_b(memory_tree, tmp_path)
    server = create_server(data_b / "catalog.sqlite", port=0,
                           token="e2e-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # 无 token → 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/sessions", timeout=5)
        assert exc.value.code == 403, "无 token 访问 API 应 403"

        # 列表可见 pull 而来的会话
        with urllib.request.urlopen(
                base + "/api/sessions?token=e2e-token", timeout=5) as resp:
            assert resp.status == 200
            listing = json.loads(resp.read())
        ids = {s["session_id"] for s in listing["sessions"]}
        assert {SID_A, SID_B} <= ids, "serve 列表应含 pull 而来的会话"

        # 详情消息字段完整
        with urllib.request.urlopen(
                base + f"/api/sessions/{SID_A}?token=e2e-token",
                timeout=5) as resp:
            assert resp.status == 200
            detail = json.loads(resp.read())
        assert len(detail["messages"]) == 2, "详情应含 2 条消息"
        first = detail["messages"][0]
        assert {"message_id", "seq", "ts", "body"} <= set(first), \
            f"消息字段不完整: {sorted(first)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
