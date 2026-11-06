"""本地工程化护栏：ruff -> mypy -> pytest 一键检查（fail-fast）。

设计见 docs/superpowers/specs/2026-11-06-架构工程化-护栏-design.md。
用项目内 .venv 的解释器执行，任意解释器均可运行本脚本：
    python scripts/check.py [--only ruff|mypy|pytest]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORDER = ["ruff", "mypy", "pytest"]
STEPS = {
    "ruff": ["-m", "ruff", "check", "."],
    "mypy": ["-m", "mypy"],
    "pytest": ["-m", "pytest", "-q"],
}


def find_venv_python() -> Path | None:
    for rel in (Path("Scripts") / "python.exe", Path("bin") / "python"):
        candidate = ROOT / ".venv" / rel
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="运行本地质量护栏（ruff/mypy/pytest）")
    parser.add_argument("--only", choices=ORDER, help="只运行单个步骤")
    args = parser.parse_args()

    python = find_venv_python()
    if python is None:
        print("未找到项目虚拟环境 .venv，请先在仓库根执行：")
        print("  python -m venv .venv")
        print('  .venv\\Scripts\\python.exe -m pip install -e ".[dev]"')
        return 2

    steps = [args.only] if args.only else ORDER
    for name in steps:
        print(f"== {name} ==")
        result = subprocess.run([str(python), *STEPS[name]], cwd=ROOT)
        if result.returncode != 0:
            print(f"xx {name} 未通过")
            return result.returncode
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
