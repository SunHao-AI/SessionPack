"""Spec 11 R3/R4：打包内容护栏（静态断言 + 发布前真构建冒烟）。

P1 实测本已正确（46 条目、无 spike/tests 泄漏），本文件锁的是"不许退化"：
`web/*` glob 被删时，源码树里 serve 照常工作、只有 pip install 后
用户点开页面才 404——只有静态比对配置与仓库清单才能提前发现。
"""

from __future__ import annotations

import fnmatch
import importlib
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import sessionpack

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _package_files() -> list[Path]:
    """包里应有的真实文件（排除构建与缓存产物）。"""
    return sorted(
        p for p in (REPO_ROOT / "sessionpack").rglob("*.py")
        if "__pycache__" not in p.parts)


def test_all_modules_packaged() -> None:
    """每个 .py 都在 include 语义内，且所在目录都有 __init__.py。"""
    patterns = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    files = _package_files()
    assert files, "不应出现包内无 .py 的异常状态"
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert any(fnmatch.fnmatch(rel, pat) for pat in patterns), \
            f"{rel} 不在 include {patterns} 内，装出来会少模块"
    for directory in {p.parent for p in files}:
        assert (directory / "__init__.py").is_file(), \
            f"{directory.relative_to(REPO_ROOT)} 缺 __init__.py，发现不到子包"


def test_web_assets_covered() -> None:
    """package-data 的 glob 必须覆盖 web 目录全部文件（治 serve 页面 404）。"""
    globs = _pyproject()["tool"]["setuptools"]["package-data"]["sessionpack"]
    web_files = sorted(p.name for p in (REPO_ROOT / "sessionpack" / "web").iterdir()
                       if p.is_file())
    assert web_files, "web 目录不应为空"
    for name in web_files:
        assert any(fnmatch.fnmatch(f"web/{name}", g) for g in globs), \
            f"web/{name} 未被 package-data {globs} 覆盖，装出来的包会缺它"


def test_no_stray_top_level_packaged() -> None:
    """tests/spike/docs/scripts 四个顶层目录不得被 include 命中。"""
    patterns = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    for stray in ("tests", "spike", "docs", "scripts"):
        hits = [pat for pat in patterns if fnmatch.fnmatch(f"{stray}/x", pat)]
        assert hits == [], f"include {hits} 会把 {stray}/ 打进发布包"


def test_console_script_importable() -> None:
    """入口点指向的 sessionpack.cli:main 必须真实可导入且可调用。"""
    spec = _pyproject()["project"]["scripts"]["sessionpack"]
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr)), f"{spec} 的目标不可调用"


@pytest.mark.skipif(not os.environ.get("SP_BUILD_WHEEL"),
                    reason="发布前冒烟：SP_BUILD_WHEEL=1 时真构建 wheel（约 20s）")
def test_wheel_smoke(tmp_path: Path) -> None:
    """端到端：真构建 wheel，校验内容清单与 dynamic 版本真正落进元数据。"""
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--no-build-isolation", "-w", str(tmp_path), str(REPO_ROOT)],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=600)
    assert built.returncode == 0, f"构建失败：{built.stdout[-800:]}{built.stderr[-800:]}"
    wheels = sorted(tmp_path.glob("sessionpack-*.whl"))
    assert len(wheels) == 1, f"应恰好产出一个 wheel：{[w.name for w in wheels]}"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        expected = {f"sessionpack/{p.relative_to(REPO_ROOT / 'sessionpack').as_posix()}"
                    for p in _package_files()}
        assert expected <= names, \
            f"包内缺模块：{sorted(expected - names)[:5]}"
        for name in sorted(p.name for p in
                           (REPO_ROOT / "sessionpack" / "web").iterdir()):
            assert f"sessionpack/web/{name}" in names, f"wheel 缺 web/{name}"
        assert not [n for n in names
                    if n.startswith(("spike/", "tests/", "docs/", "scripts/"))], \
            "wheel 混入非发布目录"
        assert any(n.endswith("LICENSE") for n in names), "wheel 缺 LICENSE"
        meta = zf.read([n for n in names if n.endswith("METADATA")][0]).decode()
        assert f"Version: {sessionpack.__version__}" in meta, \
            f"METADATA 版本与 __version__ 不符（dynamic 未生效）：{sessionpack.__version__}"
        assert wheels[0].name == \
            f"sessionpack-{sessionpack.__version__}-py3-none-any.whl", \
            f"wheel 文件名版本不符：{wheels[0].name}"
        entry = [n for n in names if n.endswith("entry_points.txt")]
        assert entry and "console_scripts" in zf.read(entry[0]).decode(), \
            "wheel 缺 console_scripts 入口"
