# Spec 1 本地工程化护栏 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 SessionPack 建立本地质量护栏：项目内 `.venv` + ruff + mypy + 一键 `scripts/check.py`，首跑全绿且零行为变更。

**Architecture:** 配置全部进现有 `pyproject.toml`（唯一配置源）；`scripts/check.py` 用 `.venv` 内解释器按 ruff → mypy → pytest 顺序 fail-fast 执行；存量违例按"--fix 优先、人工次之、禁止 noqa 刷屏"清理。

**Tech Stack:** Python 3.11+（实际 3.12.13）、ruff>=0.6、mypy>=1.8、pytest>=8，全部仅 dev 依赖。

**Spec:** `docs/superpowers/specs/2026-11-06-架构工程化-护栏-design.md`（已获用户批准）

## Global Constraints

- 对外契约硬锁：manifest format_version=1、config.toml 格式、CLI 命令参数、HTTP 端点均不变。
- 不写 CI（GitHub Actions 明确排除），只建本地护栏。
- 工具只装项目内 `.venv`，不污染 Anaconda base（D:\Anaconda3）。
- 运行时零第三方依赖不变：ruff/mypy 只进 `[project.optional-dependencies] dev`。
- 不开 `ruff format`；`line-length = 100`；`spike/` 排除 lint。
- mypy 宽基线：不开 strict、不开 disallow_untyped_defs、范围只 `sessionpack`。
- 禁止用 `# noqa` 批量压噪音；mypy 豁免只允许模块头 `# mypy: ignore-errors` + 原因注释。
- 不改任何运行逻辑；mypy 若暴露疑似真 bug，记录到 Spec 2 待办，本计划不修。
- 回归网：全量测试保持 144 passed。

**环境偏差说明（相对 spec）：** 仓库当前没有 README.md。Task 5 改为新建最小 README.md（标题 + 一句话简介 + 开发环境小节），代替"修改 README"。

---

### Task 1: pyproject 护栏配置 + 项目内 .venv

**Files:**
- Modify: `pyproject.toml`（dev 依赖行 + 文件尾追加三节）
- Create: `.venv/`（不入库，.gitignore 已含 `.venv/`）

**Interfaces:**
- Consumes: 无
- Produces: `.venv\Scripts\python.exe`（已装 ruff/mypy/pytest，Task 2 的 check.py 定位它）；`[tool.ruff]`/`[tool.mypy]` 配置（Task 3/4 的工具读取）

- [ ] **Step 1: 修改 dev 依赖**

`pyproject.toml` 第 15 行：

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6", "mypy>=1.8"]
```

- [ ] **Step 2: 文件尾追加三个配置节**

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
extend-exclude = ["spike"]

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "B", "UP", "I"]
ignore = []

[tool.mypy]
python_version = "3.11"
files = ["sessionpack"]
ignore_missing_imports = true
```

注意：现有 `[tool.pytest.ini_options]` 节保留原样，新节追加在文件末尾即可（TOML 节顺序无语义）。

- [ ] **Step 3: 创建 .venv 并安装**

在仓库根（PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

若 pip 网络失败，先检查系统代理（git 此前走代理正常；pip 用 `--proxy` 或 `HTTP(S)_PROXY` 环境变量，值与 git 代理一致）。

- [ ] **Step 4: 验证工具可用**

```powershell
.venv\Scripts\python.exe -m ruff --version
.venv\Scripts\python.exe -m mypy --version
.venv\Scripts\python.exe -m pytest --version
```

预期：三条都打印版本号，无 `No module named`。

- [ ] **Step 5: 验证配置被读取**

```powershell
.venv\Scripts\python.exe -m ruff check . --statistics
```

预期：输出违例统计（此时有存量违例属正常，Task 3 清理）；若报"无法解析 pyproject"则修 Step 2。

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml
git commit -m "chore: 加入 ruff/mypy 护栏配置与 dev 依赖"
```

`git status` 确认 `.venv/` 未出现（被 .gitignore 覆盖）。

---

### Task 2: scripts/check.py 一键检查入口

**Files:**
- Create: `scripts/check.py`

**Interfaces:**
- Consumes: Task 1 的 `.venv\Scripts\python.exe`、pyproject 中的 ruff/mypy 配置
- Produces: `python scripts/check.py`（退出码 0=全绿 / 非 0=某步失败 / 2=缺 venv）；`python scripts/check.py --only ruff|mypy|pytest`（Task 5 验收与 Spec 2 重构期高频使用）

- [ ] **Step 1: 写 scripts/check.py（完整内容如下，无占位）**

```python
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
```

- [ ] **Step 2: 冒烟运行（此时预期在 ruff 步失败，属正常）**

```powershell
python scripts/check.py
```

预期：依次打印 `== ruff ==`，透传 ruff 违例输出，`xx ruff 未通过`，退出码非 0。这验证 fail-fast 与透传。若 Task 3 前 ruff 恰好已 0 违例，则继续跑完 mypy/pytest，同样属正常。

- [ ] **Step 3: 验证 --only**

```powershell
python scripts/check.py --only pytest
echo $LASTEXITCODE
```

预期：只跑 pytest，144 passed，退出码 0。

- [ ] **Step 4: 验证缺 venv 路径（对应验收标准 3）**

```powershell
Rename-Item .venv .venv_bak
python scripts/check.py
echo $LASTEXITCODE
Rename-Item .venv_bak .venv
```

预期：打印三行创建指引，`$LASTEXITCODE` 为 2，无 traceback。**最后一条 Rename 必须执行恢复。**

- [ ] **Step 5: Commit**

```powershell
git add scripts/check.py
git commit -m "feat: 新增 scripts/check.py 本地一键护栏入口"
```

---

### Task 3: ruff 存量清零

**Files:**
- Modify: ruff 报告涉及的 `sessionpack/`、`tests/`、`scripts/` 文件（具体清单由 Step 1 统计决定）

**Interfaces:**
- Consumes: Task 1 的 `[tool.ruff]` 配置
- Produces: `ruff check .` 退出码 0（Task 5 全绿的前提）

- [ ] **Step 1: 统计存量**

```powershell
.venv\Scripts\python.exe -m ruff check . --statistics
```

记录各类违例数量与文件清单（`--statistics` 无文件列表时补跑 `ruff check .` 看明细）。

- [ ] **Step 2: 自动修复可修项**

```powershell
.venv\Scripts\python.exe -m ruff check . --fix
.venv\Scripts\python.exe -m ruff check . --statistics
```

`--fix` 覆盖 I001（导入排序）与多数 UP 规则。预期剩余数量显著下降。

- [ ] **Step 3: 人工修复剩余项**

对剩余每条违例逐个修代码本身。处理原则：
- 剩余 ≤5 处：直接改写（如 E731 lambda 改 def、E741 歧义变量名改名）。
- 修完重跑 Step 2 命令，直到 `All checks passed!`。
- **禁止**加 `# noqa`；若某条规则确实与项目冲突（如某 B 规则在 handler 场景误报），停下来向用户报告违例全文再定夺，不要擅自 ignore 整条规则。
- 若改动涉及 `sessionpack/` 行为语义（正常 lint 修复不应改变行为），停下报告。

- [ ] **Step 4: 回归测试**

```powershell
.venv\Scripts\python.exe -m pytest -q
```

预期：144 passed（导入重排/写法现代化不应改变行为；有失败说明修坏了，回滚该文件重改）。

- [ ] **Step 5: Commit**

```powershell
git add -A sessionpack tests scripts
git commit -m "style: ruff 存量违例清零（I/UP/E 子集自动+人工修复）"
```

---

### Task 4: mypy 基线跑绿

**Files:**
- Modify: mypy 报告涉及的 `sessionpack/` 文件（补注解或模块级豁免）

**Interfaces:**
- Consumes: Task 1 的 `[tool.mypy]` 配置（files=["sessionpack"]）
- Produces: `mypy` 退出码 0（Task 5 全绿的前提）

- [ ] **Step 1: 首跑采集错误清单**

```powershell
.venv\Scripts\python.exe -m mypy
```

记录全部错误（预期集中在 catalog.py 的 sqlite3.Row 下标、config.py 裸 dict 注解等，勘察估计 ≤15 条）。

- [ ] **Step 2: 逐条处理**

决策树（按优先级）：
1. **补注解**：函数签名缺注解导致 `no-untyped-def` 类连锁错误 → 补一行参数/返回注解。
2. **收窄注解**：如 `def load_config(root: Path) -> tuple[dict, list[str]]` 报 dict 类型参数缺失 → 改 `tuple[dict[str, object], list[str]]` 或更精确类型。
3. **模块级豁免（最后手段）**：确属第三方动态属性无法注解 → 该文件首行 docstring 之后加 `# mypy: ignore-errors  原因：<一句话>`。全仓库豁免文件数 ≤3。

**禁止**行内 `# type: ignore` 满天飞；**禁止**在 pyproject 加 `disable_error_code` 全局静音。
若某错误明显指向**真实 bug**（如必然 TypeError 的路径），不要修逻辑——把错误原文记入本计划文件末尾"Spec 2 待办"小节，该文件按第 3 类豁免处理，保证基线绿。

- [ ] **Step 3: 复跑确认清零**

```powershell
.venv\Scripts\python.exe -m mypy
```

预期：`Success: no issues found in 14 source files`（文件数以实际为准）。

- [ ] **Step 4: 回归测试**

```powershell
.venv\Scripts\python.exe -m pytest -q
```

预期：144 passed。

- [ ] **Step 5: Commit**

```powershell
git add -u
git commit -m "chore: mypy 宽基线跑绿（补注解+最小模块级豁免）"
```

---

### Task 5: 全绿收口 + 最小 README + 最终验收

**Files:**
- Create: `README.md`（仓库当前没有 README，本任务新建最小版）

**Interfaces:**
- Consumes: Task 1-4 全部产物
- Produces: spec 验收标准 1-5 全部达成的可交付状态

- [ ] **Step 1: 新建 README.md（完整内容如下）**

```markdown
# SessionPack

Trae 会话归档与同步：本地只读会话管理（数据不出本机）。

## 开发环境

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    python scripts/check.py

`check.py` 依次运行 ruff、mypy、pytest，任一失败即退出。可加
`--only ruff|mypy|pytest` 单跑一步。
```

- [ ] **Step 2: 全新进程全绿验证（验收标准 1、2）**

开一个**新终端**（模拟无历史状态）：

```powershell
cd e:\Workplace\SessionPack
python scripts/check.py
echo $LASTEXITCODE
python scripts/check.py --only mypy
echo $LASTEXITCODE
```

预期：两次退出码均 0；全量输出中 ruff `All checks passed!`、mypy `Success`、pytest `144 passed`。

- [ ] **Step 3: git 状态验证（验收标准 4）**

```powershell
git status --short
git status --ignored --short
```

预期：`--short` 为空；`--ignored` 中可见 `.venv/`、`.ruff_cache/`（说明 .gitignore 覆盖生效）。

- [ ] **Step 4: Commit**

```powershell
git add README.md
git commit -m "docs: 新增最小 README（开发环境与 check.py 用法）"
```

- [ ] **Step 5: 对照 spec 验收标准逐条勾验**

spec 五条验收标准对应：1→Step 2；2→Step 2；3→Task 2 Step 4 已验；4→Step 3；5→Task 3/4 Step 4 已验 + Step 2 复核。全部满足则本计划完成。

---

## Spec 2 待办（执行中发现的真 bug 记录区）

（暂无）
