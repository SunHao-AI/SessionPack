# MVP 2 设计规格：用户自选目录同步端

- 日期：2026-11-06
- 状态：已确认（用户逐节批准）
- 上游决策：C1（自选目录优先、归档包明文、端到端加密推迟 MVP 3）、C2（同步端必须支持完全本地/自托管、许可证 MIT），见《未完成任务清单》C1/C2 与构想稿第 8、11 节。
- 交付目标：构想稿 MVP 2 中的 F1「用户自选目录同步端」+ LICENSE（MIT）。

## 1. 范围

**做：**

1. CLI 新命令 `push` / `pull`：本地 `archives/` 与用户自选同步目录之间的单向推送与拉取导入。
2. 同步目录配置持久化到 `<data-root>/config.toml` 的 `[sync]` 段（新增最小配置读写，零第三方依赖）。
3. `doctor` 与诊断页新增同步目录状态（配置值、可达性、包数、总体积）。
4. `LICENSE` 文件（MIT）随本批加入。

**不做（明确排除）：**

- 端到端加密与密钥管理（MVP 3 单独设计）。
- 删除同步 / 墓碑机制（两端只增不删，误删不扩散；与构想稿 7 节"避免静默扩散"一致）。
- 冲突合并（归档包不可变、`archive_id` 由内容哈希稳定推导，同名必同内容，无真冲突场景）。
- 多同步端（本批只支持一个 `[sync] dir`；配置结构不阻碍将来扩展）。
- Web UI 同步操作页（只做 CLI + 诊断页展示）。

## 2. 命令接口

两个命令共享参数：`--data-dir`（沿用全局）、`--json`。

```text
sessionpack push [--dir DIR] [--dry-run]
sessionpack pull [--dir DIR]
```

- `--dir`：本次使用的同步目录；缺省读 `config.toml` 的 `[sync] dir`；两处都没有 → 退出码 2 并提示用 `--dir` 或先 `push --dir X` 落配置。
- `push --dir X` 成功完成后把 `X` 写入 `config.toml`（记住最近一次使用的目录）。`pull` 不写配置。
- `--dry-run`：只打印将复制的文件清单与字节数，不写任何东西。
- 目录形态：同步目录直接平铺 `<slug>-<date>-<id12>.zip`，不加子目录、不改名；同步目录不存在时 `mkdir -p` 创建（视为用户显式指定的意图）。

## 3. 数据流

### push（本地 → 同步目录，只增）

1. 列出本地 `layout.archives/*.zip` 与同步目录 `*.zip`，按文件名差集得到待推集合（文件名含 `archive_id[:12]`，同名即同内容，这是 ADR-007 稳定 ID 的直接推论）。
2. 每个待推文件先 `verify_archive()` 复验；未通过的跳过并记入报告（不把坏包推出去）。
3. 复制到同步目录：先写同目录 `.tmp-<name>` 再 `os.replace` 原子改名（与 `build_archive` 同款做法），中途失败不留半个文件。
4. 已存在的同名文件跳过（幂等）。
5. 输出 `SyncResult`：pushed / skipped_existing / skipped_unverified / bytes / errors。

### pull（同步目录 → 本地，只增）

1. 列同步目录 `*.zip`。
2. 对每个远端包：先按文件名尾部 `archive_id[:12]` 判断本地 `archives/` 是否已有同 ID 包，有则跳过（避免同日差集、跨日重名误判）。
3. 新包先在原地 `verify_archive()`：不通过 → 不动本地、记原因；通过 → 复制进本地 `archives/`（保持原名，原子复制），再走现有 `import_archive()` 导入索引（该路径已幂等，重复导入 0 新增）。
4. 输出 `SyncResult`：pulled / skipped_existing / skipped_invalid（含原因）/ sessions_added / messages_added / bytes。

错误文本一律经 `archive._failure_text` 净化（绝对路径替换为 `<路径>`），与 MVP 1.2 的口径一致。

## 4. 模块与改动点

| 单元 | 改动 | 说明 |
|---|---|---|
| `sessionpack/sync.py`（新） | `push_archives(archives_dir, sync_dir, dry_run)`、`pull_archives(archives_dir, sync_dir, conn)`、`SyncResult` dataclass | 复用 `archive.verify_archive` / `import_archive`；本模块是纯文件操作 + 既有库函数编排，不直接碰 SQLite schema |
| `sessionpack/config.py`（新） | `load_config(root)` / `set_sync_dir(root, dir)` | 读用 stdlib `tomllib`；写用最小手写序列化（只有一个键，不引入 tomli-w）；文件损坏时按空配置处理并警告，不中断命令 |
| `sessionpack/cli.py` | 注册 `push` / `pull` 子命令，接 `_cmd_push` / `_cmd_pull`；退出码沿用现有约定（0 成功、2 参数/配置错、1 运行期失败） | |
| `sessionpack/diagnostics.py` | `collect()` 增加 `sync` 段：`configured`（bool）、`dir`、`exists`、`archives`（个数）、`bytes` | 未配置时 `configured=false`，不报错 |
| `sessionpack/web/diagnostics.html` + `app.js` | 诊断页渲染 `sync` 段 | 与现有分项一致 |
| `docs/使用说明-本地只读.md` | 增补「跨设备同步」章节（命令示例、只增不删语义、明文包提醒） | |
| `LICENSE` | MIT 全文，版权行 `Copyright (c) 2026 SessionPack contributors` | 规格评审时如要署名真名，改这一行 |

不动的部分：`archive.py`、`importer.py`、`catalog.py` 的既有逻辑不改；`paths.Layout` 不加 sync 字段（同步目录是用户数据指针，不属于本工具存储布局，持久化归 config.toml）。

## 5. 配置格式

```toml
# <data-root>/config.toml
[sync]
dir = "D:\\Sync\\sessionpack"
```

读取容错：文件不存在 / 非法 TOML / 缺 `[sync]` → 视为未配置。写入只重写 `[sync]` 段所在的完整文件（当前仅这一个键）。

## 6. 错误处理与安全边界

- 同步目录指向任意路径是用户显式意图，但 `push`/`pull` 只读写 `*.zip` 与本工具命名模式的临时文件，不遍历、不删除同步目录中的其他文件。
- 读取边界：`pull` 的导入走 `import_archive` 既有路径，语义与 `import --archive` 一致；`push`/`pull` 不提供 `--audit` 旗标（读取源只有本地 `archives/` 与同步目录中的 zip，无源目录遍历）。
- 明文归档包进入网盘/NAS 挂载盘属用户自担范围（C2 决策已载明），使用说明中再次显著提示。
- DNS rebinding、令牌等 Web 层边界不涉及（本批不新增 HTTP 面）。

## 7. 测试与验收

沿用 pytest + tmp_path 合成样本，无外部依赖：

1. `tests/test_sync.py`：
   - push 新建同步目录、首推全部、复推 0 新增（幂等）；
   - push 跳过未通过 verify 的坏包并计数；
   - push 中断不留 `.tmp-*` 残留（模拟复制异常）；
   - pull 复制+自动导入往返：设备 A push → 设备 B pull → B 可检索该会话，导出与 A 逐字节一致（复用 test_acceptance 模式）；
   - pull 重复执行 0 新增；坏包跳过且不动本地；同 ID 不同文件名跳过去重；
   - `--dry-run` 零写入；错误文本不含绝对路径。
2. `tests/test_cli.py`：push/pull 未配置目录退出码 2、`--dir` 首用落配置、`--json` 输出结构。
3. `tests/test_server.py`：诊断接口 `sync` 段渲染（未配置/已配置两态）。
4. 全量回归：`python -m pytest -q` 全绿（当前基线 110 passed，新增后以实测为准）。

验收口径：两台"设备"（两个 data-root + 一个共享 tmp 目录）完成 push → pull → 检索 → 导出一致性演练，作为 acceptance 测试固化。

## 8. LICENSE（MIT）

- 随本批添加 `LICENSE` 文件，MIT 全文。
- 依据 C2：零运行时依赖、无第三方合规负担，无需依赖许可证清单。
- `pyproject.toml` 的 `license` 字段同步声明 MIT（若当前缺省）。
