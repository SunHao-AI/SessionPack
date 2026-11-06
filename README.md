# SessionPack

Trae 会话归档与同步：本地只读会话管理（数据不出本机）。

## 开发环境

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    python scripts/check.py

`check.py` 依次运行 ruff、mypy、pytest，任一失败即退出。可加
`--only ruff|mypy|pytest` 单跑一步。
