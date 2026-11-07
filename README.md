# SessionPack

Trae 会话归档与同步：本地只读会话管理（数据不出本机）。

## 使用

    sessionpack import        # 导入本机 Trae 记忆目录，建本地索引
    sessionpack serve         # 起本地 Web UI 浏览与检索
    sessionpack push --dir D:\share   # 把归档包导出到同步目录

完整用法见[使用说明-本地只读](docs/使用说明-本地只读.md)。同步目录由
`push --dir` 落盘到数据目录下的 `config.toml`（`[sync].dir`），属工具内部
状态文件，不建议手写。

## 开发环境

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    python scripts/check.py

`check.py` 依次运行 ruff、mypy、pytest，任一失败即退出。可加
`--only ruff|mypy|pytest` 单跑一步。
