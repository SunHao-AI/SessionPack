# SessionPack

Trae 会话归档与同步：本地只读会话管理（数据不出本机）。

## 使用

    sessionpack import        # 导入本机 Trae 记忆目录，建本地索引
    sessionpack serve         # 起本地 Web UI 浏览与检索
    sessionpack delete --session ID    # 软删除会话（墓碑，随归档包传播不复活；--undo 仅撤销本机；--purge --yes 物理清除本机内容，不可逆）
    sessionpack push --dir D:\share   # 把归档包导出到同步目录（--encrypt 端到端加密）

唯一运行时第三方依赖：`cryptography`（同步加密）；其余功能纯标准库。

完整用法见[使用说明-本地只读](docs/使用说明-本地只读.md)。同步目录由
`push --dir` 落盘到数据目录下的 `config.toml`（`[sync].dir`），属工具内部
状态文件，不建议手写。

### 索引体检与修复

    sessionpack doctor                # 体检：FTS 漂移会在"索引对账"行检出并给出修复命令
    sessionpack rebuild --fts         # 只重建全文索引，快且无风险
    sessionpack rebuild --from-archives --yes   # 从归档包全量重建，换入前原库备份为 .bak

索引库是纯派生缓存，一切不一致问题用 `rebuild` 解决；`--from-archives`
以 `archives/*.zip` 为唯一数据源，会丢弃未归档的会话，故需 `--yes` 确认。

### 消息检索

    sessionpack search 关键词            # 消息级检索：≥3 字走全文索引，其余 LIKE
    sessionpack search 关键词 --json     # 结构化输出（session_id / message_id / body）

## 开发环境

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    python scripts/check.py

`check.py` 依次运行 lint_docs、lint_imports、ruff、mypy、pytest 五步，
任一失败即退出。可加 `--only lint_docs|lint_imports|ruff|mypy|pytest`
单跑一步。

发布前冒烟（真构建 wheel 并校验内容，约 20 秒，日常跳过）：

    $env:SP_BUILD_WHEEL = "1"; python scripts/check.py --only pytest
