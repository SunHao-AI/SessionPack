"""Spec 11 R2：版本单一来源与 --version 一致性护栏。

版本双声明在 git 史上真出事过（eb96499 专门"统一版本号到 0.3.0"），
本文件把"只有一处声明"从约定升级为断言。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import sessionpack

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_version_single_source() -> None:
    """[project] 不得再出现硬编码 version；声明必须指向 __version__。"""
    cfg = _pyproject()
    project = cfg["project"]
    assert "version" not in project, \
        "pyproject 的 [project].version 必须删除（改回硬编码即双声明复发）"
    assert project["dynamic"] == ["version"], "version 必须声明为 dynamic"
    attr = cfg["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "sessionpack.__version__", \
        f"dynamic 版本源必须是 __version__，实际：{attr}"


def test_version_string_shape() -> None:
    """__version__ 必须是模块级三段数字字面量（setuptools AST 静态读的硬前提）。"""
    raw = (REPO_ROOT / "sessionpack" / "__init__.py").read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.startswith("__version__")]
    assert len(lines) == 1, f"__version__ 赋值应恰好一处，实际 {len(lines)} 处"
    assert lines[0] == f'__version__ = "{sessionpack.__version__}"', \
        "必须是模块级字符串字面量（函数调用/拼接会让 setuptools 静态读失败）"
    assert re.fullmatch(r"\d+\.\d+\.\d+", sessionpack.__version__), \
        f"版本串应为三段数字，实际：{sessionpack.__version__}"


def test_dash_version_matches_package() -> None:
    """用户可见的 --version 必须等于包内 __version__（否则 pip show 与 CLI 两个答案）。"""
    res = subprocess.run(
        [sys.executable, "-m", "sessionpack", "--version"],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=90)
    assert res.returncode == 0, f"--version 应退 0: {res.stderr}"
    assert res.stdout.strip() == sessionpack.__version__, \
        f"--version 输出 {res.stdout.strip()!r} != __version__"
