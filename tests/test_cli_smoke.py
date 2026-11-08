"""Spec 8 E2E-2：真实 `python -m sessionpack` 子进程冒烟。

全部测试走真实入口薄壳：argparse 参数表、stdout/stderr 编码、
退出码传导——这些环节坏了只有子进程测试能发现。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import write_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    """统一子进程入口：cwd 固定仓库根，UTF-8 双保险，60s 超时必须失败而非挂死。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "sessionpack", *argv],
        cwd=str(REPO_ROOT), env=env, capture_output=True,
        encoding="utf-8", timeout=60,
    )


def _minimal_tree(tmp_path: Path) -> Path:
    """最小记忆树：1 项目 1 会话 2 消息，够铺满 import/doctor/export。"""
    root = tmp_path / "memory"
    sid = "c" * 24
    write_jsonl(
        root / "projects" / "-d-Work-RepoX" / "20260103"
        / f"session_memory_{sid}.jsonl",
        [
            {"message_id": "m1", "message_summary_time": "2026-01-03 09:00:00",
             "intent": "冒烟", "outcome": "子进程入口正常",
             "actions": [], "learned": []},
            {"message_id": "m2", "message_summary_time": "2026-01-03 09:05:00",
             "intent": "退出码", "outcome": "用法错误应退 2",
             "actions": [], "learned": []},
        ],
    )
    return root


def test_help_exits_zero_with_usage_text() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "Trae 会话归档与同步" in proc.stdout
    assert "import" in proc.stdout and "rebuild" in proc.stdout


def test_unknown_subcommand_exits_usage() -> None:
    proc = _run("nope")
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_import_missing_memory_root_exits_usage(tmp_path: Path) -> None:
    proc = _run("import", "--memory-root", str(tmp_path / "nope"),
                "--data-dir", str(tmp_path / "data"))
    assert proc.returncode == 2
    assert "记忆目录不存在" in proc.stderr


def test_import_then_doctor_json_roundtrip(tmp_path: Path) -> None:
    """薄壳 --json 输出必须是可解析 JSON，stats 键齐全。"""
    tree, data = _minimal_tree(tmp_path), tmp_path / "data"
    imp = _run("import", "--memory-root", str(tree),
               "--data-dir", str(data), "--json")
    assert imp.returncode == 0, imp.stderr
    imported = json.loads(imp.stdout)
    assert imported["sessions_created"] == 1
    assert imported["messages_inserted"] == 2

    doc = _run("doctor", "--memory-root", str(tree),
               "--data-dir", str(data), "--json")
    assert doc.returncode == 0, doc.stderr
    stats = json.loads(doc.stdout)["catalog"]["stats"]
    assert stats == {"projects": 1, "sessions": 1, "messages": 2,
                     "fts_rows": 2}


def test_export_archive_then_verify_ok(tmp_path: Path) -> None:
    """export --archive --all 落盘后，verify 真实 zip 应退 0。"""
    tree, data = _minimal_tree(tmp_path), tmp_path / "data"
    assert _run("import", "--memory-root", str(tree),
                "--data-dir", str(data)).returncode == 0
    exp = _run("export", "--archive", "--all", "--data-dir", str(data),
               "--out", str(data / "archives"))
    assert exp.returncode == 0, exp.stderr
    zips = sorted((data / "archives").glob("*.zip"))
    assert len(zips) == 1, f"应导出 1 个项目包: {zips}"

    ver = _run("verify", str(zips[0]))
    assert ver.returncode == 0, ver.stdout + ver.stderr
    assert "OK" in ver.stdout


def test_rebuild_fts_json_reports_int(tmp_path: Path) -> None:
    tree, data = _minimal_tree(tmp_path), tmp_path / "data"
    assert _run("import", "--memory-root", str(tree),
                "--data-dir", str(data)).returncode == 0
    proc = _run("rebuild", "--fts", "--data-dir", str(data), "--json")
    assert proc.returncode == 0, proc.stderr
    rebuilt = json.loads(proc.stdout)["rebuilt"]
    assert isinstance(rebuilt, int) and rebuilt == 2
