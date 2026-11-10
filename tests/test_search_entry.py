"""Spec 10 R4：消息级检索的三入口（CLI 子进程 / HTTP API / Web 静态契约）。

既有 test_e2e_flow.py 的检索断言是进程内直调 catalog.search_messages，
本文件只补"真实入口 → 函数"的连通与渲染契约，全部新增、不动既有断言。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from sessionpack import catalog
from sessionpack.cli.read_cmds import run_search
from sessionpack.cli.write_cmds import run_import
from sessionpack.server import create_server
from tests.conftest import SID_A, SID_B

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "sessionpack" / "web"


def _ns(**kwargs) -> argparse.Namespace:
    """构造与 parser 等价的 args 对象（与 test_e2e_flow 同法，测试间不互导）。"""
    base = dict(memory_root=None, data_dir=None, json=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _imported(memory_tree: Path, data_dir: Path) -> Path:
    """导入夹具记忆树，返回数据目录（各用例独立 tmp_path，互不污染）。"""
    res = run_import(_ns(memory_root=str(memory_tree), data_dir=str(data_dir),
                         json=True, archive=None, project=None, since=None,
                         until=None, reparse=False, retry_failed=False,
                         audit=False))
    assert res.exit_code == 0, f"夹具导入失败: {res.err_lines}"
    return data_dir


def _cli(*argv: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    """走真实 CLI 子进程（与 Spec 8/9 手法：仓库根 cwd + UTF-8 双保险）。"""
    return subprocess.run(
        [sys.executable, "-m", "sessionpack", *argv,
         "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=90)


def test_run_search_hit(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 1：run_search 命中与文本渲染（对齐 e2e 步骤 2 的精确命中）。"""
    data = _imported(memory_tree, tmp_path / "device")
    res = run_search(_ns(data_dir=str(data), term="校验", limit=50, json=True))
    assert res.exit_code == 0, f"命中检索应退 0: {res.err_lines}"
    assert res.data["term"] == "校验"
    assert res.data["count"] == 1
    assert [(h["session_id"], h["message_id"]) for h in res.data["hits"]] \
        == [(SID_A, "m2")], "入口检索应与 e2e 步骤 2 的直调结果同锁"
    text = run_search(_ns(data_dir=str(data), term="校验", limit=50, json=False))
    assert text.out_lines[0] == '消息命中 1 条（检索词"校验"）'
    assert text.out_lines[1].startswith(f"{SID_A} #m2  校验归档"), \
        f"文本行应含定位前缀+摘要: {text.out_lines}"


def test_run_search_boundaries(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 2：空词/索引缺失退 2，未命中退 0（检索无果不是错误）。"""
    data = _imported(memory_tree, tmp_path / "device")
    blank = run_search(_ns(data_dir=str(data), term="   ", limit=50, json=True))
    assert blank.exit_code == 2 and "检索词不能为空" in blank.err_lines
    missing = run_search(_ns(data_dir=str(tmp_path / "void"), term="校验",
                             limit=50, json=True))
    assert missing.exit_code == 2 and any(
        "索引不存在" in line for line in missing.err_lines), \
        "索引未就绪应退 2（沿 cmd_serve 文案）"
    empty = run_search(_ns(data_dir=str(data), term="量子隧穿", limit=50,
                           json=True))
    assert empty.exit_code == 0 and empty.data["count"] == 0
    text = run_search(_ns(data_dir=str(data), term="量子隧穿", limit=50,
                          json=False))
    assert "未命中" in text.out_lines[0]


def _set_titles(data: Path) -> None:
    """把标题/摘要改成与夹具正文无关的受控文案，隔离会话检索的断言。"""
    conn = catalog.connect(data / "catalog.sqlite")
    try:
        conn.execute("UPDATE sessions SET title='增量导入的幂等性验证',"
                     " summary='重复执行导入不产生重复行'"
                     " WHERE session_id=?", (SID_B,))
        conn.commit()
    finally:
        conn.close()


def test_run_search_session_title_and_summary(
    memory_tree: Path, tmp_path: Path
) -> None:
    """入口层 3b：标题/摘要命中的会话级检索（消息无果时仍能定位会话）。"""
    data = _imported(memory_tree, tmp_path / "device")
    _set_titles(data)
    res = run_search(_ns(data_dir=str(data), term="幂等", limit=50, json=True))
    assert res.exit_code == 0
    assert res.data["count"] == 0, "标题命中不应混入消息命中"
    assert [(h["session_id"], h["title"]) for h in res.data["session_hits"]] \
        == [(SID_B, "增量导入的幂等性验证")]
    sub = run_search(_ns(data_dir=str(data), term="重复行", limit=50, json=True))
    assert [h["session_id"] for h in sub.data["session_hits"]] == [SID_B], \
        "仅摘要含词的会话也要命中"
    assert sub.data["session_hits"][0]["summary"] == "重复执行导入不产生重复行"
    text = run_search(_ns(data_dir=str(data), term="幂等", limit=50, json=False))
    assert text.out_lines[0].startswith("消息命中 0 条"), text.out_lines
    assert any(line.startswith(f"{SID_B}  增量导入") for line in text.out_lines), \
        text.out_lines


def test_run_search_session_tombstone_hidden(
    memory_tree: Path, tmp_path: Path
) -> None:
    """入口层 3c：墓碑会话不出现在会话命中（与消息命中同一隐藏规则）。"""
    data = _imported(memory_tree, tmp_path / "device")
    _set_titles(data)
    conn = catalog.connect(data / "catalog.sqlite")
    try:
        assert catalog.mark_session_deleted(conn, SID_B, now="2026-03-05T10:00:00")
        conn.commit()
    finally:
        conn.close()
    res = run_search(_ns(data_dir=str(data), term="幂等", limit=50, json=True))
    assert res.exit_code == 0 and res.data["session_hits"] == []
    text = run_search(_ns(data_dir=str(data), term="幂等", limit=50, json=False))
    assert "未命中任何结果" in text.out_lines[0]


def test_run_search_limit_clamped(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 3：limit 夹取 1..500（0 与负数收敛为 1，与 API 同规则）。"""
    data = _imported(memory_tree, tmp_path / "device")
    zero = run_search(_ns(data_dir=str(data), term="会话", limit=0, json=True))
    assert zero.exit_code == 0 and zero.data["limit"] == 1
    assert len(zero.data["hits"]) <= zero.data["limit"]
    huge = run_search(_ns(data_dir=str(data), term="会话", limit=99999,
                          json=True))
    assert huge.data["limit"] == 500


def test_cli_search_json_subprocess(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 4：真实子进程 search --json，stdout 纯 JSON 且命中精确。"""
    data = _imported(memory_tree, tmp_path / "device")
    res = _cli("search", "校验", "--json", data_dir=data)
    assert res.returncode == 0, f"CLI 应退 0: {res.stderr}"
    payload = json.loads(res.stdout)
    assert [(h["session_id"], h["message_id"]) for h in payload["hits"]] \
        == [(SID_A, "m2")]
    miss = _cli("search", "量子隧穿", "--json", data_dir=data)
    assert miss.returncode == 0 and json.loads(miss.stdout)["count"] == 0


def test_cli_search_missing_catalog_subprocess(tmp_path: Path) -> None:
    """入口层 5：空数据目录 → 退 2、stderr 有文案、stdout 无 JSON（emit 契约）。"""
    res = _cli("search", "校验", "--json", data_dir=tmp_path / "void")
    assert res.returncode == 2
    assert "索引不存在" in res.stderr
    assert res.stdout.strip() == "", "失败路径 data=None，--json 下 stdout 应为空"


def _serve(data: Path):
    server = create_server(data / "catalog.sqlite", port=0, token="s10-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _get(base: str, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_api_search(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 6：/api/search 的 token/参数契约（200/400/403 + limit 生效）。"""
    data = _imported(memory_tree, tmp_path / "device")
    server, thread, base = _serve(data)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                base + "/api/search?q=" + urllib.parse.quote("校验"), timeout=5)
        assert exc.value.code == 403, "无 token 应 403"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/search?token=s10-token", timeout=5)
        assert exc.value.code == 400, "缺 q 应 400"
        quoted = urllib.parse.quote("校验")
        status, payload = _get(
            base, f"/api/search?token=s10-token&q={quoted}")
        assert status == 200
        assert [(h["session_id"], h["message_id"]) for h in payload["hits"]] \
            == [(SID_A, "m2")]
        status, capped = _get(
            base, f"/api/search?token=s10-token&q={urllib.parse.quote('会话')}"
                  f"&limit=1&limit=junk")
        assert status == 200 and len(capped["hits"]) <= 1, \
            "limit 首个值生效，非法值不致 500"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_and_cli_consistent(memory_tree: Path, tmp_path: Path) -> None:
    """入口层 7：同一库同一词，API 与 run_search 的 hits 全等（同一函数）。"""
    data = _imported(memory_tree, tmp_path / "device")
    server, thread, base = _serve(data)
    try:
        _status, api_payload = _get(
            base, "/api/search?token=s10-token&q=" + urllib.parse.quote("会话"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    cli_res = run_search(_ns(data_dir=str(data), term="会话", limit=50,
                             json=True))
    assert api_payload["hits"] == cli_res.data["hits"], \
        "两入口必须收敛到 search_messages 的同一结果"


def test_web_entry_wired() -> None:
    """入口层 8：Web 接线的存在性锁（静态读文件，不起浏览器）。"""
    index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    session = (WEB_DIR / "session.html").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'id="hits"' in index and 'id="hit_list"' in index, \
        "首页应有消息命中区"
    assert "/api/search" in app, "app.js 应调用消息级端点"
    assert 'params.get("q")' in app, "首页应从 URL 回填检索词"
    assert 'id="msg_search"' in session, "详情页应有跳回首页的检索入口"
