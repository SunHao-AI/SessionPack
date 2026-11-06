# 架构工程化 Spec 3：CLI 拆分与遗留清偿设计

日期：2026-11-06
状态：待用户审阅
前置：Spec 2（模块边界）已落地并推送 origin/main，HEAD `e0eb011`，
`python scripts/check.py` 全绿（167 passed），lint_imports BASELINE 清零。

## 1. 背景与目标

Spec 2 §7 备案的遗留项在本 spec 一次清偿，主体是 cli.py 上帝模块
（347 行、8 依赖、8 子命令；json/text 双输出与目录前置检查模式重复，
打印逻辑嵌入 handler 导致输出无法直接单测）。

已确认的四项决策：

1. 范围 = cli.py 拆分（主体）+ Spec 2 §7 四项小遗留全收（含 catalog
   读写分离）+ 守卫加固 Minor；**不纳入**配置体系与文档工程化。
2. 拆分目标 = 可测性 + 去重（业务编排与输出生成分离）。
3. 契约 = 退出码、JSON 字段、CLI 参数锁死；人类可读 text 措辞可微调
   （既有测试断言同步更新，在同一 commit 内）。
4. 拆分形态 = 方案 A：`cli/` 包（仿 archive 拆包先例，`__init__` 重导出保契约）。

### 硬约束（延续 Spec 1/2）

- 对外契约硬锁：`sessionpack.cli.main/build_parser/EXIT_*` 导入路径、
  `sessionpack.catalog.X` 全部公共符号、`python -m sessionpack`、
  CLI 命令与参数、JSON 输出字段名、退出码表（0/1/2/3）均不变。
- 零运行时第三方依赖（守卫脚本仅 stdlib）；不写 CI；`spike/` 不动。
- 167 个既有测试保持全绿；断言更新仅限 text 措辞微调，不改语义。

## 2. 实证问题清单

| # | 问题 | 证据 |
|---|------|------|
| C1 | cli.py 上帝模块 | 347 行、顶部 8 依赖；`build_parser()` 约 70 行与 8 个 handler 混居 |
| C2 | json/text 双输出重复 | doctor/scan/import/verify/push/pull 6 处 `if args.json: print(json.dumps(...)) else: print(...)` |
| C3 | 前置检查重复 | `_catalog_ready` 在 serve/export 重复；"未指定同步目录"提示在 push/pull 重复 |
| C4 | 输出嵌入 handler 不可测 | 打印散布在 handler 中段（如 `_cmd_export` 循环内 print），单测只能 capsys 抓 stdout |
| C5 | 手写 mode=ro 连接两处 | cli.py L31-33、diagnostics.py L78-79 各自拼 `file:...?mode=ro` URI |
| C6 | server 重导出 | server.py L18 重导出 `catalog.SESSION_ID_RE` |
| C7 | sync 相对导入 | sync.py `from . import archive` 与全库绝对导入风格不一致 |
| C8 | catalog.py 324 行读写混居 | 连接/schema/读查询/写 API 四段混居单文件 |
| C9 | 守卫加固 4 项 | ledger 备案：E_PRIVATE 别名逃逸；E_SQL docstring 误报；未映射模块静默免检 E_LAYER；点式 import 属性链盲区 |

## 3. 目标模块结构

```
sessionpack/
├── catalog/                 ← C8：单文件拆为包（读写分离）
│   ├── __init__.py          ← 重导出全部公共符号（含 SESSION_ID_RE），
│   │                          sessionpack.catalog.X 对外路径不变
│   ├── conn.py              ← connect / connect_readonly（新增）/ init_schema /
│   │                          has_schema / WAL 与 schema 迁移等连接与 schema 原语
│   ├── read.py              ← get_projects / list_sessions / get_session /
│   │                          messages / search / stats 等只读查询
│   └── write.py             ← upsert_*、import_runs / import_files 全部写 API
├── cli/                     ← C1：单文件拆为包（方案 A）
│   ├── __init__.py          ← 重导出 main / build_parser / EXIT_OK/ERROR/USAGE/VERIFY
│   ├── parser.py            ← build_parser()：8 子命令 argparse 集中一处
│   ├── common.py            ← layout_for / memory_root_for / require_catalog（C3/C5：
│   │                          内部用 catalog.connect_readonly + has_schema）/
│   │                          resolve_sync_dir
│   ├── output.py            ← L0 例外登记：仅 stdlib；emit(res, as_json) 统一渲染
│   ├── read_cmds.py         ← doctor / scan / verify
│   └── write_cmds.py        ← import / export / serve / push / pull
```

`__main__.py`（若存在）与 `pyproject` 入口点指向 `sessionpack.cli:main`，
经 `__init__` 重导出后不变。

### 3.1 分层规则表变化（lint_imports.py LAYER_OF）

| 变化 | 说明 |
|------|------|
| 新增 `conn: 2, read: 2, write: 2` | catalog 包内文件按子名映射 L2；包内互引已被"同包豁免"规则覆盖（archive 先例） |
| 新增 `parser: 4, common: 4, read_cmds: 4, write_cmds: 4` | cli 包内文件映射 L4 |
| 新增 `output: 0` | 强制 output.py 只依赖 stdlib——若它 import 任何 sessionpack 模块即触发 E_LAYER L0→L2 |
| E_SQL 豁免改 | `rel == "sessionpack/catalog.py"` → `rel.startswith("sessionpack/catalog/")` |

## 4. 逐项设计

### 4.1 C1–C4：cli/ 包与 CommandResult 渲染模式

每个子命令拆成两层：

```
run_xxx(...) -> CommandResult     # 业务编排：调领域模块，不 print、不感知 as_json
cmd_xxx(args) -> int              # 入口：run_xxx(...) 后交 output.emit 渲染，返回退出码
```

`output.py`（仅 stdlib）：

```python
@dataclass
class CommandResult:
    exit_code: int
    data: dict | list | None      # --json 视图；None 表示该路径无 JSON 输出
    out_lines: list[str]          # text 模式 stdout
    err_lines: list[str]          # text 模式 stderr

def emit(res: CommandResult, as_json: bool) -> int:
    # as_json 且 data 非 None：print(json.dumps(data, ensure_ascii=False, indent=2))
    # 否则打印 out_lines → stdout、err_lines → stderr
    # data 为 None 时（如"索引不存在"类前置错误），无论 as_json 与否都走 err_lines
    # ——与现状行为一致（这些错误现在就是直接 print 到 stderr）
    return res.exit_code
```

去重效果：C2 的六处 `if args.json` 收敛为 `emit` 一处；C3 的
`require_catalog` / `resolve_sync_dir` 进 common.py，错误提示文案一处定义。

可测性：`run_xxx` 返回 CommandResult 可直接断言 `exit_code/data/out_lines`；
text 措辞改动由这些单测覆盖，不再依赖 capsys。

`_cmd_serve` 特例：serve 无数据视图，run_serve 返回
`CommandResult(EXIT_OK, None, [], [])` 前直接阻塞启动 server（现状行为）。

### 4.2 C5：catalog.connect_readonly

```python
def connect_readonly(path: Path) -> sqlite3.Connection:
    # sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    # 文件不存在/不可开时抛 sqlite3.Error，由调用方处理；调用方负责 close
```

- diagnostics.py L78-79 改调 `catalog.connect_readonly`。
- cli `_catalog_ready` 逻辑迁入 `cli/common.require_catalog(path) -> bool`：
  try connect_readonly + catalog.has_schema，捕 `sqlite3.Error`/
  `sqlite3.DatabaseError` 返回 False（与现行为等价）。
- serve/export 改调 `require_catalog`。

### 4.3 C6/C7：两处一行修复

- server.py L18：删除 `from sessionpack.catalog import SESSION_ID_RE` 重导出
  行；若 server 内部使用则改为用时直接从 catalog 导入。实施时先 grep
  `server.SESSION_ID_RE` 确认无外部消费者（测试若有引用一并改）。
- sync.py：`from . import archive` → `from sessionpack import archive`。

### 4.4 C8：catalog 拆包细则

- 按函数名归位：conn（connect/connect_readonly/init_schema/has_schema/
  SCHEMA 常量与迁移）、read（get_projects/list_sessions/get_session/
  get_messages/search/stats 等 SELECT）、write（upsert_*、begin/finish_import_run、
  load/register/reset/clear/save_import_file* 等写 API）。
- `SESSION_ID_RE` 定义在 conn.py（read/write 共用校验），`__init__` 重导出。
- read.py / write.py 只允许从 conn.py 与 L0 导入（守卫自然强制）。
- 对外零变化：既有 `from sessionpack import catalog; catalog.xxx` 全部经
  `__init__` 重导出可达；测试 import 路径不改。

### 4.5 C9：守卫加固 4 项（lint_imports.py）

1. **E_PRIVATE 别名逃逸**：`ast.Import` 分支补检——`import sessionpack.a._b`
   或 `import sessionpack.a._b as c` 中任一 sessionpack 后段 `_` 前缀即报
   E_PRIVATE（当前只查 ImportFrom）。
2. **E_SQL docstring 误报**：`sql_violations` 收集模块/类/函数的 docstring
   常量节点 id 并排除；受管表写语句出现在 docstring 不再报。
3. **未映射模块免检**：sessionpack 下未登记 LAYER_OF 且非 `__init__` 门面
   的模块——按 **L0 处理**（最严：向上 import 即 E_LAYER 报违规），并向
   stderr 打印 `W_UNMAPPED 模块 'x' 未登记层级，按 L0 处理`，强制新模块
   显式登记。
4. **点式属性链盲区**：属性访问检查从"直接 Name"扩展为"沿 Attribute 链
   解析根 Name"——根经 imported_names 解析为 sessionpack 模块且链上任一
   段 `_` 前缀、目标模块 ≠ 当前模块时报 E_PRIVATE。若产生既有代码
   误报，逐条评估后仅允许进 BASELINE（预期为零）。

每项一条先红后绿的 lint_imports 单测（tests/test_lint_imports.py 增 4 例）。

## 5. 迁移节奏（每步 commit，护栏链全绿）

| Commit | 内容 | 涉及 C |
|--------|------|--------|
| S1 | catalog/ 拆包 + `connect_readonly` + diagnostics/cli `_catalog_ready` 改用 + E_SQL 豁免适配 + LAYER_OF 新增 catalog 子名 | C5 C8 |
| S2 | server 重导出删除 + sync 绝对导入 | C6 C7 |
| S3 | cli/ 拆包 + CommandResult/emit 渲染模式 + 输出措辞微调（如有）+ run_xxx 单测 | C1–C4 |
| S4 | 守卫加固 4 项 + 4 条 lint 单测 + W_UNMAPPED | C9 |
| S5 | 收尾：全量回归、§7 条目状态回写本文档 | — |

顺序理由：S1 先行提供 `connect_readonly`，S3 的 `require_catalog` 直接落在
新形态上，避免 cli 代码改两遍。

## 6. 测试与验收

- `python scripts/check.py` 全绿（167 基线 + 新增用例）。
- 新增：每个子命令 1 条 `run_xxx` → CommandResult 断言（至少 exit_code 与
  data 关键字段）；lint_imports 加固 4 例；catalog 包公共符号重导出冒烟 1 例。
- 契约核验清单（S5 执行）：`python -m sessionpack --help` 与 8 子命令
  `--help` 参数面 diff 为零；`python -c "from sessionpack.cli import main,
  build_parser, EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_VERIFY"`；
  `python -c "import sessionpack.catalog as c; ..."` 对全部原公共符号 hasattr。

## 7. 已知遗留（本 spec 不做）

- 配置体系与文档工程化（用户裁定不纳入）。
- output.py 被领域层 import 无法被 E_LAYER 拦截（L0 向下不受层级规则约束），
  以代码评审约定"output 仅 cli/* 使用"兜底。
- 属性链 E_PRIVATE 扩展启发式对实例属性链的误报可能，以 BASELINE 兜底。
- `owner.endswith(source_module)` 是后缀启发式而非段边界匹配（`lint_imports.py`
  三处同源），理论上名为 `x.catalog` 的包会被误判为同包而豁免；基线既有形态，
  本次未扩大其使用范围。
- 实施实测偏差与契约核验记录（2026-11-06，计划
  `docs/superpowers/plans/2026-11-06-cli拆分与遗留清偿.md`）：
  - `catalog.py` 实际 378 行（本设计初稿记为 324）、`cli.py` 实际 391 行（初稿记为
    347）；拆包按实际行数完成，不影响方案。
  - `server.py` 对 `SESSION_ID_RE` 的依赖是**赋值式重导出**
    （`SESSION_ID_RE = catalog.SESSION_ID_RE`），且模块内部另有属性式调用点；已按
    「删除重导出 + 内部改用 `catalog.SESSION_ID_RE`」清偿，未保留兼容别名。
  - `sync.py` 的相对导入实际为两行（`from . import archive` 与
    `from .textutil import failure_text`），本设计初稿只列了一行；两行均已改绝对导入。
  - `serve` 保持 `cmd_serve` 直返 `int`（不产出 `CommandResult`），因为它是阻塞式
    HTTP 服务，无渲染语义。
  - `main()` 的派发从 `args.func(args)` 改为
    `globals()[f"_cmd_{args.command}"]` 门面查表，以保留既有
    `monkeypatch.setattr(cli, "_cmd_scan", …)` 测试写法。
  - 全量测试最终计数：`195 passed`（基线 167；计划预算 189）。差额 +6 全部落在
    C9 守卫所在的 `tests/test_lint_imports.py`（13 → 23）：计划预算四项各 1 条，
    实做 10 条——四项守卫各落地（E_PRIVATE import 别名 1、E_SQL 跳过 docstring 1、
    W_UNMAPPED 拆 2 条、属性链 1）+ E_SQL 豁免收窄 1（随 Task 1 的 `f9149ca` 落地）+
    扩充① 前缀键 2 + 扩充② 子包门面 1 + 修复轮 1；Task 1（5）、Task 2（1）、
    Task 3（12）与预算一致。
  - E_SQL 豁免最终形态：计划原文是整目录前缀豁免
    `rel.startswith("sessionpack/catalog/")`，经审查收紧为精确文件集合
    `SQL_WRITE_ALLOWED = {"sessionpack/catalog/conn.py", "sessionpack/catalog/write.py"}`
    （`conn.py` 因 SCHEMA 建表 DDL 必须豁免）；`read.py` 与 `catalog/__init__.py`
    恢复受 E_SQL 检查，收尾实测两处均可报违规。
  - 守卫扩充两项（用户裁定，超出计划四项）：① `LAYER_OF` 改用包前缀键
    （`catalog.conn: 2`、`cli.output: 0` 等），`_layer_of()` 先查 `包.子模块` 再回退
    包自身，消除不同包同名子模块的跨包串扰；② 子包 `__init__.py` 归一化为所属包后
    纳入 E_LAYER 检查（原先完全脱离分层检查）。
  - E_PRIVATE 同包豁免：`import a.b._c` 别名分支与 `from a.b import _c` 分支判据已
    对齐（`not owner.endswith(source_module) and owner != "sessionpack"`），同包私有
    模块可用、跨包锁死。
  - 计划样例与守卫实现细节存在三处出入（样例 `h._load()` 不触发新规则、
    `id(node.value)` 对 `ast.Module` 抛 AttributeError、无条件降级会误报顶层门面），
    均按「先红后绿」实跑校准后落地，未放宽任何规则。
  - 契约核验通过：`sessionpack.catalog.__all__` 26 项（覆盖拆分前全部 22 个公共
    顶层符号，另含 `SCHEMA`/`SESSION_ID_RE`/`connect_readonly`/`require_catalog`）；
    `sessionpack.cli` 的 `main`/`build_parser`/`EXIT_OK`/`EXIT_VERIFY` 可导入；
    `--version` → `0.3.0` 退出码 0；`scan` 缺失记忆目录 → stderr
    `记忆目录不存在: …` 退出码 2；`serve` 未建索引 → stderr
    `索引不存在，请先运行 sessionpack import` 退出码 2。临时目录 `.spike-check`
    已清理，`git status --short` 干净。
  - 两处预期与实测不符，均为**核验文案写错，未放宽规则**：
    - 计划步骤 4 第一条探针 `check_source('from sessionpack import cli\n',
      'sessionpack/catalog/read.py')` 预期 `[]`，实测报
      `E_LAYER import L2 -> L4 (catalog -> cli)`——该源码并非「catalog 包内边」，
      而是真实的 L2→L4 上向依赖，基线守卫同样报错。改以真正的包内边探针核验：
      `from . import conn` 与 `from sessionpack.catalog import conn` 均返回 `[]`，
      包内边豁免生效；E_SQL 探针报 1 条、E_PRIVATE 探针报 2 条（同一跨包私有导入被
      「import 别名」与「from import」两条规则各判一次），规则均在生效。
    - 计划步骤 3 预期在本机默认环境不成立：默认记忆目录存在，故
      `doctor --data-dir <空目录>` 退出码 0 并打印 `记忆目录：…（存在）`，须加
      `--memory-root .\does-not-exist` 才复现预期 `（不存在）` + 退出码 2；
      同理默认数据目录已建索引，`export --all --out …` 退出码 0
      （`已导出 9 个会话、47 条消息`），须补 `--data-dir .spike-check` 才复现
      `索引不存在，请先运行 sessionpack import` + 退出码 2。属环境差异，非契约破坏。
