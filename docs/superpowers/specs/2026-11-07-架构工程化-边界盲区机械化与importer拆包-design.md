# 架构工程化 Spec 4：边界盲区机械化与 importer 拆包

日期：2026-11-07
状态：待用户审阅
前置：Spec 3（cli 拆分与遗留清偿）已全部落地并推送，基线
`python scripts/check.py` 全绿（196 passed），BASELINE 空集，HEAD `b25c4b9`。

## 1. 背景与目标

Spec 2 建立分层规则表与守卫三规则（E_LAYER / E_PRIVATE / E_SQL），
Spec 3 清偿了五项遗留并加固守卫。本 spec 处理 Spec 3 §7 记录在案的两处
边界盲区（把人工兜底变成机械守卫），并清偿唯一的体量红线越线者：

1. **E_DOM_IMPORT**：`cli/output.py` 被刻意下沉为 L0（`cli.output: 0`），
   E_LAYER 只拦"向上依赖"（tgt_layer > my_layer），对领域层 import 它
   完全放行，目前仅靠代码评审约定"output 仅 cli/* 使用"兜底。
2. **E_PRIVATE 两项加固**：属性链启发式对实例属性链可能误报
   （如名为 `export` 的实例调 `._render()`），此前以 BASELINE 兜底——
   但 BASELINE 已清零，再误报将无缓冲；且缺行级豁免手段。
3. **importer.py 拆包**：实测 362 行，是 Spec 2 DoD
   "archive/ 包外无文件超过 320 行"的唯一越线者。

### 硬约束（延续 Spec 1–3）

- 对外契约硬锁：`sessionpack.importer.X` 公共 import 路径不变
  （实测 10 个 import 点：`cli/write_cmds.py` + 9 个测试文件/助手）；
  CLI 命令、HTTP 端点、manifest、config.toml 均不触碰。
- 零运行时第三方依赖（守卫脚本仅 stdlib）。
- BASELINE 保持空集：新规则落地即全绿，不得借基线缓冲。
- 不写 CI；`spike/` 不动；main 本地 commit。
- 196 个既有测试保持全绿，不删不改断言。

## 2. E_DOM_IMPORT：渲染层包内专属

仿 E_SQL 的豁免集合模式，语义从"受管表写仅限 catalog"对偶为
"渲染层 import 仅限 cli 包"：

```python
RENDER_TARGET = "sessionpack.cli.output"
RENDER_ALLOWED_PREFIX = "sessionpack/cli/"
```

- 实现位置：`check_source` 的层级边遍历循环内（复用 `_abs_from_import`
  已解析的绝对模块点路径，相对导入天然覆盖）。解析出的目标模块等于
  `RENDER_TARGET` 或其子模块，而源文件 `rel` 不以 `RENDER_ALLOWED_PREFIX`
  开头 → 违规 `E_DOM_IMPORT`。
- 与 E_SQL 的豁免形态差异（有意为之）：E_SQL 用精确文件集（冻结清单，
  防豁免面扩大）；本规则用**包前缀**（语义是"包内归属"，未来 cli/
  新增子模块天然合法，无需维护清单）。
- 现状全仓仅 `cli/read_cmds.py`、`cli/write_cmds.py` import 它（同包合法），
  落地即绿。
- 语义宣告：output 是 CLI 的领域渲染层而非公共 L0；其他层产生渲染需求时，
  正确做法是把该需求挪进 CLI 层，而非豁免。

## 3. E_PRIVATE 加固：行级豁免 + 实例遮蔽收紧

### 3.1 行级豁免注释

- 源码行尾含 `# lint-imports: allow-private` 时，跳过该行号上的
  E_PRIVATE 判定。仅豁免 E_PRIVATE，不豁免 E_LAYER / E_DOM_IMPORT / E_SQL。
- 属性链跨多行书写时，判定行号取 `ast.Attribute` 节点的 `lineno`
  （即属性名所在行），注释须落在该行。
- 用途：误报出现时精准放行单行，BASELINE 保持空集不被污染。
  豁免行属评审关注点（脚本输出中以警告列出被豁免行，防止静默滥用）。

### 3.2 实例遮蔽启发式

- 误报根因：属性链检查以"根名在 `imported_names` 中"为模块引用判据，
  但同名实例（`export = make_exporter()` 后调 `export._render()`）
  同样命中。
- 收紧：解析文件时收集**所有被重新绑定过的名字**（赋值目标、函数参数、
  for/with/except-as 目标、增广赋值等）。属性链根名若在其中，说明该名字
  在文件内至少一处不是模块引用，无法静态断定，跳过 E_PRIVATE 判定
  （保守放行）。
- 取舍（记录在案）：保守放行意味着"先 import 模块、后又把同名重绑定为
  实例、再跨模块访问私有属性"的真实违例会被漏掉——该类模式极罕见且
  本身就是坏味道，接受。
- from-import 私有名、import 别名两路检查不受本收紧影响（直接命中导入
  语句本身，无遮蔽歧义）。

## 4. importer/ 物理拆包

照 archive 先例：`__init__.py` 重导出全部公共符号，10 个既有 import 点
零改动，`from sessionpack.importer import import_source` 路径不变。

```
sessionpack/importer/
├── __init__.py   ← 重导出 import_source, ImportStats, BATCH_ROWS, MAX_FAILURES
├── state.py      ← ImportStats / _FileState / _RunProgress + _now / _relative / _selected（约 110 行）
├── plan.py       ← _plan_files / _commit_progress（约 95 行）
└── run.py        ← import_source 主编排 / _process_file / _finish_run（约 160 行）
```

- 纯搬移：`import_source` 签名、返回值、逐分支行为等价，以 196 测试
  全绿为等价证明；不改任何逻辑。
- 包内互导（相对导入或模块名式绝对导入）合法（守卫同包段边界豁免，
  顶层扁平模块不享此豁免）；跨段符号（`_FileState` 等）
  经 `state.py` 显式 import。
- `LAYER_OF` 无需新增键：`importer.plan` / `importer.run` / `importer.state`
  经 `_layer_of()` 前缀键查不到后回落到 `importer: 3`（Spec 3 扩充①机制）。
- E_SQL 不受影响：拆包后包内仍无裸 SQL（Spec 2 已收敛至 catalog）。

## 5. backlog 校正与已知遗留

- **校正**：backlog 条目"server.py 未登记 LAYER_OF"与现状不符——
  `LAYER_OF` 现含 `"server": 4` 与 `"__main__": 4`（Spec 3 实施中已登记）。
  该盲区不存在，在本文档记录校正，不回改已归档 spec。
- 已知遗留（本 spec 不做）：
  - §3.2 保守放行带来的理论漏报面（见取舍）。
  - 配置体系与文档工程化（用户此前裁定不纳入，维持）。
  - `RENDER_ALLOWED_PREFIX` 是前缀匹配：名为 `clix/` 的兄弟包理论上
    以字符串比较不会被误放（前缀含尾斜杠），但若未来出现
    `sessionpack/cli_ext/` 之类命名需重新审视归属。

## 6. 迁移顺序与验收

commit 序（每步 `python scripts/check.py` 全绿才进下一步）：

| commit | 内容 | 测试增量 |
|--------|------|---------|
| C1 | E_DOM_IMPORT 规则 + 3 测试（包外违规 / cli 内合法 / 相对导入违规） | +3 |
| C2 | E_PRIVATE 行级豁免 + 实例遮蔽收紧 + 4 测试（遮蔽豁免 / 未遮蔽仍报 / 注释豁免 / 注释加在无违规行无副作用） | +4 |
| C3 | importer/ 拆包 + 门面 hasattr 测试 | +1 |
| C4 | 文档校正（backlog）与收尾回归 | 0 |

Definition of Done：

- `check.py` 四步全绿，196 + 8 测试通过；BASELINE 保持空集。
- `importer/` 包内每文件 < 220 行；`sessionpack.importer` 门面
  hasattr 全部原公共符号。
- cli/ 包外对 `sessionpack.cli.output` 的 import 为 0 处（E_DOM_IMPORT
  机械保证，非 grep 约定）。
- `grep -r "lint-imports: allow-private" sessionpack/` 为 0 处
  （本 spec 落地时不应有任何豁免需求，出现即说明收紧失败，需回看设计）。

## 7. 实施落地记录

实施采 SDD（subagent-driven-development）逐 commit 闭环：实施者 →
任务审查者（spec 合规 + 代码质量）→ ledger 记录。commit 号取自
`git log --oneline` 实况。

| commit | 内容 | 审查结论 |
|--------|------|---------|
| `338f35c` | C1 E_DOM_IMPORT：渲染层包内专属 + 3 测试 | Approved |
| `be94369` | C2 E_PRIVATE 行级豁免 + 遮蔽场景保守放行 + 4 测试 | Approved |
| `48c4c46` | C3 importer/ 拆包（state 88 / plan 95 / run 206 行，`__init__` 门面 7 行）+ lint_imports 同包豁免配套 + 1 测试 | Approved |
| 本次 | C4 DoD 核查、Task 3 审查 I-1 收窄、守卫回归测试补 2、backlog 校正、本节 | — |

**DoD 实测**：`python scripts/check.py` 四步全绿（206 passed，
ruff / mypy 干净）；BASELINE == `set()`；`git grep
"lint-imports: allow-private" -- sessionpack/` 为 0 处；
`git grep "cli.output" -- sessionpack/` 仅 `cli/read_cmds.py:18` 与
`cli/write_cmds.py:24` 两处（均在 cli/ 包内）。测试数
196 → 199（C1）→ 203（C2）→ 204（C3）→ 206（C4 补守卫回归）。

**计划外但必要的实现改动**：`scripts/lint_imports.py` 的 E_PRIVATE
from-import 同包豁免新增 `or owner.rpartition(".")[0] == pkg`
（commit `48c4c46`）——拆包后包内绝对导入（`from
sessionpack.importer.state import _FileState`）的 owner 是完整模块路径，
原条件只比模块键会误报。Task 3 审查 I-1 指出该条件对顶层扁平模块过宽
（`pkg` 恒为 `sessionpack`），已在 C4 收窄为附加 `and pkg !=
"sessionpack"`，并补 `test_flat_module_private_import_still_checked_after_I1`
与 `test_package_intra_module_absolute_private_import_exempt` 两条守卫
回归测试锁定两侧行为。§4 措辞随之精确化。

**§5 backlog 校正**：backlog 中「server.py 未登记 LAYER_OF」条目与实际
清单文件不符——`LAYER_OF` 已含 `"server": 4` 与 `"__main__": 4`，
该盲区不存在，按 §5 结论在本文档记录校正，不回改已归档 spec。

**已知遗留（移交 Final Review 裁决，未在本轮修）**：

- P1（C1 审查，Important，计划强制）：E_DOM_IMPORT 可被
  `from sessionpack.cli import output` 门面写法绕过，现状仓内 0 处。
- P2（C2 审查，Important，计划强制）：`_rebound_names` 为文件级判定，
  单个同名函数参数即可令该文件该模块的私有检查整体失效；另有 marker
  裸行扫描可被字符串字面量误触（评审判 Minor）。
- C1/C2/C3 审查累计 10+1 条 Minor 的 deferred 清单见
  `.superpowers/sdd/2026-11-07-边界盲区机械化与importer拆包/progress.md`
  与各 `task-N-review.md`。
