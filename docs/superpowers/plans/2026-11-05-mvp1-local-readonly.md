# SessionPack MVP 1（本地只读管理）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个 Python CLI + 本地 Web UI，能对 Trae CN 明文记忆目录做只读扫描、导入、检索、导出、归档与校验。

**Architecture:** 适配器层只读声明白名单内的文件并产出归一化记录；导入器跨文件累积批量写入 `catalog.sqlite`（四张领域表 + FTS5 trigram）；归档层按 ADR-007 生成 zip + `manifest.json`；摘要层用确定性模板把会话转成可审阅 Markdown；Web 层只读本地索引，绑定 `127.0.0.1` 并校验一次性令牌。

**Tech Stack:** Python 3.11+（实测 3.12.13）、标准库（sqlite3 / zipfile / http.server / argparse）、pytest 8.4（仅开发依赖）。无运行时第三方依赖。

## Global Constraints

- 平台：首发仅 Windows；所有文本读写显式 `encoding="utf-8"`，路径用 `pathlib.Path`。
- 源数据只读：任意扫描/导入后源目录写入 0 次、文件哈希不变。
- 读取边界：实际打开的文件集合必须包含于 `adapter.declared_paths()`（`--audit` 下越界即失败）；白名单包含 `topics.md`、`session_memory_*.jsonl`、各项目 `project_memory.md`、根目录 `user_profile.md`；**绝不打开** `trae-jwt-token`、账号目录、`database.db` 内容。
- 批量写入：跨文件累积，达到 2000 行或运行结束时提交；**禁止按文件 flush**（压测显示会产生 6696 次事务、吞吐 0.55 MB/s）。行数据与 `import_files.offset` 必须同事务提交。
- 全文索引：FTS5 `tokenize='trigram'`；查询词长度 < 3 时回退 `LIKE`。
- 归档：标准 zip + 根 `manifest.json`；`archive_id` 由清单内文件哈希按 `path` 稳定排序后合并取 sha256；先写临时文件、校验通过后 `os.replace`。
- 失败项记录使用相对路径（`path.relative_to(root).as_posix()`）。
- 领域表固定为 `projects`、`sessions`、`messages`、`import_runs` 四张；`import_files` 是断点续传的实现表，不计入领域模型。
- 日志与报告不写入会话正文、密钥或代码片段；失败原因截断到 60 字符。
- 性能基线（27.83 MB / 6706 文件合成集）：首次导入 ≤ 101 s、峰值 RSS ≤ 70 MB、索引 ≤ 源数据 4 倍、检索 P95 ≤ 1 s。
- 数据目录默认 `%LOCALAPPDATA%\SessionPack`，可用 `--data-dir` 覆盖；URL 仅绑定 `127.0.0.1`。
- 包名 `sessionpack`，CLI 入口 `sessionpack`，命令行仅六个子命令：`doctor`、`scan`、`import`、`serve`、`export`、`verify`。

## File Structure

```text
pyproject.toml                       # 打包、入口点、pytest 配置
sessionpack/
  __init__.py                        # 版本号
  __main__.py                        # python -m sessionpack
  paths.py                           # 数据目录布局与默认记忆根
  model.py                           # 归一化记录与检查结果数据类
  adapters/
    __init__.py                      # get_adapter()
    base.py                          # 适配器协议
    trae_memory.py                   # Trae CN 明文记忆目录适配器
  audit.py                           # 文件打开审计（读取边界）
  catalog.py                         # 表结构、幂等写入原语、查询
  mem.py                             # 峰值 RSS 采样
  importer.py                        # 批量导入、断点、ImportRun
  archive.py                         # 归档构建、校验、导入
  summary.py                         # 确定性续接摘要
  export.py                          # Markdown / JSON 导出
  diagnostics.py                     # doctor / 诊断页所需的只读统计
  cli.py                             # 六个子命令
  server.py                          # 本地 HTTP 服务与 API
  web/
    index.html                       # 会话列表
    session.html                     # 会话详情
    diagnostics.html                 # 存储诊断
    app.js                           # 三页共用脚本
    style.css                        # 样式
tests/
  conftest.py                        # 合成记忆目录夹具
  helpers/crash_import.py            # 子进程强杀辅助脚本
  test_paths.py
  test_adapter_trae_memory.py
  test_catalog.py
  test_importer.py
  test_archive.py
  test_summary.py
  test_cli.py
  test_server.py
  test_acceptance.py
```

---

### Task 1: 工程骨架与数据目录布局

**Files:**
- Create: `pyproject.toml`
- Create: `sessionpack/__init__.py`
- Create: `sessionpack/__main__.py`
- Create: `sessionpack/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: 无。
- Produces: `sessionpack.paths.Layout`（字段 `root`、`catalog`、`objects`、`archives`、`temp`、`config`），`ensure_layout(root: Path) -> Layout`，`resolve_data_dir(value: str | None) -> Path`，`default_memory_root() -> Path`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_paths.py
from pathlib import Path

from sessionpack.paths import default_memory_root, ensure_layout, resolve_data_dir


def test_ensure_layout_creates_subdirs(tmp_path: Path) -> None:
    layout = ensure_layout(tmp_path)
    assert layout.catalog == tmp_path / "catalog.sqlite"
    assert layout.archives.is_dir()
    assert layout.objects.is_dir()
    assert layout.temp.is_dir()
    assert not layout.catalog.exists()


def test_resolve_data_dir_prefers_argument(tmp_path: Path) -> None:
    assert resolve_data_dir(str(tmp_path)) == tmp_path.resolve()


def test_default_memory_root_points_to_trae_cn() -> None:
    assert default_memory_root().name == "memory"
    assert default_memory_root().parent.name == ".trae-cn"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_paths.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack'`

- [ ] **Step 3: 写最小实现**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "sessionpack"
version = "0.1.0"
description = "Trae 会话归档与同步：本地只读会话管理"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
sessionpack = "sessionpack.cli:main"

[tool.setuptools.packages.find]
include = ["sessionpack*"]

[tool.setuptools.package-data]
sessionpack = ["web/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```python
# sessionpack/__init__.py
__version__ = "0.1.0"
```

```python
# sessionpack/__main__.py
from sessionpack.cli import main

raise SystemExit(main())
```

```python
# sessionpack/paths.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SessionPack"


@dataclass(frozen=True)
class Layout:
    root: Path
    catalog: Path
    objects: Path
    archives: Path
    temp: Path
    config: Path


def default_data_dir() -> Path:
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or str(Path.home() / ".local" / "share")
    )
    return Path(base) / APP_NAME


def resolve_data_dir(value: str | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_data_dir()


def ensure_layout(root: Path) -> Layout:
    layout = Layout(
        root=root,
        catalog=root / "catalog.sqlite",
        objects=root / "objects",
        archives=root / "archives",
        temp=root / "temp",
        config=root / "config.toml",
    )
    for directory in (root, layout.objects, layout.archives, layout.temp):
        directory.mkdir(parents=True, exist_ok=True)
    return layout


def default_memory_root() -> Path:
    return Path.home() / ".trae-cn" / "memory"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_paths.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml sessionpack tests/test_paths.py
git commit -m "chore: 建立 Python 工程骨架与数据目录布局"
```

---

### Task 2: 归一化模型与 Trae 记忆目录适配器

**Files:**
- Create: `sessionpack/model.py`
- Create: `sessionpack/adapters/__init__.py`
- Create: `sessionpack/adapters/base.py`
- Create: `sessionpack/adapters/trae_memory.py`
- Create: `tests/conftest.py`
- Test: `tests/test_adapter_trae_memory.py`

**Interfaces:**
- Consumes: `sessionpack.paths` 无依赖；仅用标准库。
- Produces:
  - `model.ProjectRecord(project_id, display_name, local_path_hint, source_kind)`
  - `model.TopicRecord(session_id, topic_time, body, project_id, source_kind)`
  - `model.MessageRecord(session_id, message_id, seq, ts, intent, outcome, actions, learned, meta, body, project_id, source_kind)`
  - `model.SourceFile(path, project_id, raw_project_dir, kind)`，`kind` 为 `"topics" | "messages"`
  - `model.InspectResult`，含 `as_dict()`
  - `adapters.TraeMemoryAdapter`：`id="trae-cn-memory"`、`version=1`、`source_kind="trae-memory"`
  - `TraeMemoryAdapter.declared_paths(root) -> list[Path]`
  - `TraeMemoryAdapter.enumerate(root) -> Iterator[SourceFile]`
  - `TraeMemoryAdapter.read_file(ref, start_offset=0, start_seq=0) -> Iterator[tuple[int, Record | None, str | None]]`
  - `adapters.get_adapter() -> TraeMemoryAdapter`

- [ ] **Step 1: 写失败测试与共享夹具**

```python
# tests/conftest.py
import json
from pathlib import Path

import pytest

SID_A = "a" * 24
SID_B = "b" * 24


def _row(message_id: str, ts: str, intent: str, outcome: str,
         actions: list[str], learned: list[str]) -> dict:
    return {
        "message_id": message_id,
        "message_summary_time": ts,
        "intent": intent,
        "outcome": outcome,
        "actions": actions,
        "learned": learned,
    }


def write_jsonl(path: Path, rows: list[dict], extra_raw: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if extra_raw is not None:
        text += extra_raw
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def memory_tree(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    suffixed = root / "projects" / "-d-Work-RepoA--p2-0123456789abcdef"
    plain = root / "projects" / "-d-Work-RepoA"
    other = root / "projects" / "-d-Work-RepoB"

    day_a = suffixed / "20260101"
    day_b = plain / "20260102"
    day_c = other / "20260102"

    day_a.mkdir(parents=True)
    (day_a / "topics.md").write_text(
        f"[session_id: {SID_A} | topic_summary_time: 2026-01-01 10:00:00]实现归档包的校验流程\n"
        "这一行不是合法记录\n",
        encoding="utf-8",
    )
    write_jsonl(
        day_a / f"session_memory_{SID_A}.jsonl",
        [
            _row("m1", "2026-01-01 10:00:00", "导入会话", "完成基线导入",
                 ["读取 topics.md"], ["批量提交比逐文件提交快"]),
            _row("m2", "2026-01-01 10:05:00", "校验归档", "校验通过",
                 ["计算 sha256"], ["清单是唯一可信来源"]),
        ],
    )
    write_jsonl(
        day_b / f"session_memory_{SID_B}.jsonl",
        [
            _row("m1", "2026-01-02 09:00:00", "整理项目", "生成续接摘要",
                 ["提取涉及文件"], ["摘要必须确定性生成"]),
            _row("m2", "2026-01-02 09:10:00", "补充测试", "覆盖率提升",
                 [], []),
        ],
        extra_raw="这不是 JSON\n",
    )
    (day_c / "topics.md").parent.mkdir(parents=True)
    (day_c / "topics.md").write_text(
        f"[session_id: {SID_B} | topic_summary_time: 2026-01-02 11:00:00]跨设备检索准备\n",
        encoding="utf-8",
    )

    (plain / "project_memory.md").write_text("项目长期记忆\n", encoding="utf-8")
    (suffixed / "project_memory.md").write_text("项目长期记忆（旧分片）\n", encoding="utf-8")
    (root / "user_profile.md").write_text("用户偏好\n", encoding="utf-8")
    (root / "trae-jwt-token").write_text("secret-token", encoding="utf-8")
    return root
```

```python
# tests/test_adapter_trae_memory.py
from pathlib import Path

from sessionpack.adapters import TraeMemoryAdapter
from tests.conftest import SID_A, SID_B


def test_declared_paths_include_whitelist_and_exclude_token(memory_tree: Path) -> None:
    declared = TraeMemoryAdapter().declared_paths(memory_tree)
    names = sorted(p.name for p in declared)
    assert names.count("project_memory.md") == 2
    assert "user_profile.md" in names
    assert "topics.md" in names
    assert "trae-jwt-token" not in names
    assert len(declared) == 7


def test_enumerate_merges_suffixed_project_dirs(memory_tree: Path) -> None:
    files = list(TraeMemoryAdapter().enumerate(memory_tree))
    assert {f.project_id for f in files} == {"-d-Work-RepoA", "-d-Work-RepoB"}
    assert {f.kind for f in files} == {"topics", "messages"}
    assert len(files) == 4


def test_inspect_reports_counts_and_dates(memory_tree: Path) -> None:
    info = TraeMemoryAdapter().inspect(memory_tree)
    assert info.adapter_id == "trae-cn-memory"
    assert info.project_count == 2
    assert info.project_dir_count == 3
    assert info.topic_files == 2
    assert info.jsonl_files == 2
    assert info.bytes_total > 0
    assert (info.date_min, info.date_max) == ("2026-01-01", "2026-01-02")


def test_read_file_parses_topics_and_skips_bad_lines(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(f for f in adapter.enumerate(memory_tree) if f.kind == "topics")
    records = [rec for _, rec, err in adapter.read_file(ref) if rec]
    errors = [err for _, _, err in adapter.read_file(ref) if err]
    assert len(records) == 1
    assert records[0].session_id == SID_A
    assert records[0].body == "实现归档包的校验流程"
    assert errors == ["topic format mismatch"]


def test_read_file_resumes_with_continuing_sequence(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(
        f for f in adapter.enumerate(memory_tree)
        if f.kind == "messages" and f.path.name == f"session_memory_{SID_A}.jsonl"
    )
    first = list(adapter.read_file(ref))
    second = list(adapter.read_file(ref, first[0][0], start_seq=1))
    assert [r.seq for _, r, _ in first if r] == [1, 2]
    assert [r.seq for _, r, _ in second if r] == [2]
    assert second[0][1].message_id == "m2"


def test_read_file_records_failure_for_broken_json(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(
        f for f in adapter.enumerate(memory_tree)
        if f.kind == "messages" and f.path.name == f"session_memory_{SID_B}.jsonl"
    )
    results = list(adapter.read_file(ref))
    assert len([r for _, r, _ in results if r]) == 2
    assert len([e for _, _, e in results if e]) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_adapter_trae_memory.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.adapters'`

- [ ] **Step 3: 写模型与适配器**

```python
# sessionpack/model.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FileKind = Literal["topics", "messages"]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    local_path_hint: str | None
    source_kind: str


@dataclass(frozen=True)
class TopicRecord:
    session_id: str
    topic_time: str
    body: str
    project_id: str
    source_kind: str


@dataclass(frozen=True)
class MessageRecord:
    session_id: str
    message_id: str
    seq: int
    ts: str | None
    intent: str | None
    outcome: str | None
    actions: tuple[str, ...]
    learned: tuple[str, ...]
    meta: dict | None
    body: str
    project_id: str
    source_kind: str


Record = TopicRecord | MessageRecord


@dataclass(frozen=True)
class SourceFile:
    path: Path
    project_id: str
    raw_project_dir: str
    kind: FileKind


@dataclass(frozen=True)
class InspectResult:
    adapter_id: str
    adapter_version: int
    source_kind: str
    root: Path
    project_count: int
    project_dir_count: int
    topic_files: int
    jsonl_files: int
    bytes_total: int
    date_min: str | None
    date_max: str | None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_kind": self.source_kind,
            "root": str(self.root),
            "projects": self.project_count,
            "project_dirs": self.project_dir_count,
            "topic_files": self.topic_files,
            "jsonl_files": self.jsonl_files,
            "bytes_total": self.bytes_total,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "notes": list(self.notes),
        }
```

```python
# sessionpack/adapters/base.py
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol

from sessionpack.model import InspectResult, Record, SourceFile


class MemoryAdapter(Protocol):
    id: str
    version: int
    source_kind: str

    def detect(self, root: Path) -> bool: ...

    def inspect(self, root: Path) -> InspectResult: ...

    def declared_paths(self, root: Path) -> list[Path]: ...

    def enumerate(self, root: Path) -> Iterator[SourceFile]: ...

    def read_file(
        self, ref: SourceFile, start_offset: int = 0, start_seq: int = 0
    ) -> Iterator[tuple[int, Record | None, str | None]]: ...
```

```python
# sessionpack/adapters/__init__.py
from sessionpack.adapters.trae_memory import TraeMemoryAdapter

__all__ = ["TraeMemoryAdapter", "get_adapter"]


def get_adapter() -> TraeMemoryAdapter:
    return TraeMemoryAdapter()
```

```python
# sessionpack/adapters/trae_memory.py
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from sessionpack.model import (
    InspectResult,
    MessageRecord,
    Record,
    SourceFile,
    TopicRecord,
)

TOPIC_RE = re.compile(
    r"^\[session_id: ([0-9a-f]{24}) \| topic_summary_time: "
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\](.*)$"
)
SUFFIX_RE = re.compile(r"--p\d+-[0-9a-f]{16}$")
SESSION_FILE_RE = re.compile(r"^session_memory_([0-9a-f]{24})\.jsonl$")


def canonical_project_dir(name: str) -> str:
    return SUFFIX_RE.sub("", name)


def project_display_name(canonical: str) -> str:
    parts = canonical.lstrip("-").split("-")
    if len(parts) < 2:
        return canonical
    return f"{parts[0].upper()}:\\" + "\\".join(parts[1:])


class TraeMemoryAdapter:
    id = "trae-cn-memory"
    version = 1
    source_kind = "trae-memory"

    def detect(self, root: Path) -> bool:
        return root.is_dir() and (root / "projects").is_dir()

    def declared_paths(self, root: Path) -> list[Path]:
        declared: list[Path] = []
        profile = root / "user_profile.md"
        if profile.is_file():
            declared.append(profile)
        projects = root / "projects"
        if not projects.is_dir():
            return declared
        for project in sorted(projects.iterdir()):
            if not project.is_dir():
                continue
            memory = project / "project_memory.md"
            if memory.is_file():
                declared.append(memory)
            for day in sorted(project.iterdir()):
                if not day.is_dir():
                    continue
                topics = day / "topics.md"
                if topics.is_file():
                    declared.append(topics)
                for candidate in sorted(day.iterdir()):
                    if candidate.is_file() and SESSION_FILE_RE.match(candidate.name):
                        declared.append(candidate)
        return declared

    def enumerate(self, root: Path) -> Iterator[SourceFile]:
        for path in self.declared_paths(root):
            if path.name == "topics.md":
                raw = path.parent.parent.name
                yield SourceFile(path, canonical_project_dir(raw), raw, "topics")
            elif SESSION_FILE_RE.match(path.name):
                raw = path.parent.parent.name
                yield SourceFile(path, canonical_project_dir(raw), raw, "messages")

    def inspect(self, root: Path) -> InspectResult:
        declared = self.declared_paths(root)
        sources = [f for f in self.enumerate(root)]
        topics = [f for f in sources if f.kind == "topics"]
        jsonls = [f for f in sources if f.kind == "messages"]
        total = 0
        for path in declared:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        days = sorted(
            path.parent.name
            for path in declared
            if path.parent.name.isdigit() and len(path.parent.name) == 8
        )
        dates = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
        notes: list[str] = []
        merged = {f.project_id for f in sources}
        if len({f.raw_project_dir for f in sources}) > len(merged):
            notes.append("已按 --pN-<hash> 后缀归并同名项目目录")
        return InspectResult(
            adapter_id=self.id,
            adapter_version=self.version,
            source_kind=self.source_kind,
            root=root,
            project_count=len(merged),
            project_dir_count=len({f.raw_project_dir for f in sources}),
            topic_files=len(topics),
            jsonl_files=len(jsonls),
            bytes_total=total,
            date_min=dates[0] if dates else None,
            date_max=dates[-1] if dates else None,
            notes=tuple(notes),
        )

    def _session_id(self, ref: SourceFile) -> str:
        match = SESSION_FILE_RE.match(ref.path.name)
        if match is None:
            raise ValueError("invalid session file name")
        return match.group(1)

    def read_file(
        self, ref: SourceFile, start_offset: int = 0, start_seq: int = 0
    ) -> Iterator[tuple[int, Record | None, str | None]]:
        session_id = self._session_id(ref) if ref.kind == "messages" else None
        offset = start_offset
        seq = start_seq
        with ref.path.open("rb") as handle:
            handle.seek(start_offset)
            for raw in handle:
                offset += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    yield offset, None, None
                    continue
                if ref.kind == "topics":
                    match = TOPIC_RE.match(line)
                    if match is None:
                        yield offset, None, "topic format mismatch"
                        continue
                    yield offset, TopicRecord(
                        session_id=match.group(1),
                        topic_time=match.group(2),
                        body=match.group(3).strip(),
                        project_id=ref.project_id,
                        source_kind=self.source_kind,
                    ), None
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                    message_id = payload.get("message_id")
                    if not isinstance(message_id, str) or not message_id:
                        raise ValueError("missing message_id")
                    actions = payload.get("actions")
                    learned = payload.get("learned")
                    actions = [x for x in actions if isinstance(x, str)] if isinstance(actions, list) else []
                    learned = [x for x in learned if isinstance(x, str)] if isinstance(learned, list) else []
                    intent = payload.get("intent") if isinstance(payload.get("intent"), str) else None
                    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), str) else None
                    ts = payload.get("message_summary_time")
                    ts = ts if isinstance(ts, str) else None
                    meta = payload.get("compact_summary_meta")
                    meta = meta if isinstance(meta, dict) else None
                except (ValueError, json.JSONDecodeError) as exc:
                    yield offset, None, str(exc)[:60]
                    continue
                seq += 1
                body = " ".join([x for x in [intent, outcome, *actions, *learned] if x])
                yield offset, MessageRecord(
                    session_id=session_id,
                    message_id=message_id,
                    seq=seq,
                    ts=ts,
                    intent=intent,
                    outcome=outcome,
                    actions=tuple(actions),
                    learned=tuple(learned),
                    meta=meta,
                    body=body,
                    project_id=ref.project_id,
                    source_kind=self.source_kind,
                ), None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_adapter_trae_memory.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/model.py sessionpack/adapters tests/conftest.py tests/test_adapter_trae_memory.py
git commit -m "feat: 实现 Trae 记忆目录只读适配器与白名单"
```

---

### Task 3: catalog.sqlite 表结构与幂等写入原语

**Files:**
- Create: `sessionpack/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `sessionpack.model.ProjectRecord`、`TopicRecord`、`MessageRecord`。
- Produces:
  - `catalog.connect(path: Path) -> sqlite3.Connection`（`row_factory=sqlite3.Row`，WAL）
  - `catalog.init_schema(conn) -> str`（返回实际分词器名）
  - `catalog.WriteResult(inserted: bool, session_created: bool)`
  - `catalog.upsert_project(conn, project) -> None`
  - `catalog.upsert_topic(conn, rec) -> WriteResult`
  - `catalog.upsert_message(conn, rec) -> WriteResult`
  - `catalog.refresh_session_hashes(conn, session_ids: set[str]) -> None`
  - `catalog.search_messages(conn, term: str, limit: int = 50) -> list[sqlite3.Row]`
  - `catalog.list_sessions(conn, **filters) -> list[dict]`
  - `catalog.get_session(conn, session_id) -> dict | None`
  - `catalog.get_messages(conn, session_id) -> list[dict]`
  - `catalog.get_projects(conn) -> list[dict]`
  - `catalog.stats(conn) -> dict[str, int]`
  - `catalog.tokenizer_of(conn) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_catalog.py
from pathlib import Path

from sessionpack import catalog
from sessionpack.model import MessageRecord, TopicRecord


def _msg(body: str, message_id: str = "m1", seq: int = 1,
         ts: str = "2026-01-01 10:00:00") -> MessageRecord:
    return MessageRecord(
        session_id="a" * 24, message_id=message_id, seq=seq, ts=ts,
        intent="导入会话", outcome=body, actions=("读取文件",),
        learned=("批量提交更快",), meta=None, body=body,
        project_id="-d-Work-RepoA", source_kind="trae-memory",
    )


def test_init_schema_creates_tables_and_trigram(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    assert catalog.init_schema(conn) == "trigram"
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"projects", "sessions", "messages", "import_runs",
            "import_files", "message_fts"} <= names
    assert catalog.tokenizer_of(conn) == "trigram"


def test_upsert_message_is_idempotent_and_indexed(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    first = catalog.upsert_message(conn, _msg("归档包的校验流程已完成"))
    second = catalog.upsert_message(conn, _msg("归档包的校验流程已完成"))
    assert first == catalog.WriteResult(True, True)
    assert second == catalog.WriteResult(False, False)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 1
    hits = catalog.search_messages(conn, "归档包")
    assert len(hits) == 1 and hits[0]["session_id"] == "a" * 24


def test_search_falls_back_to_like_for_two_char_term(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("校验通过"))
    assert len(catalog.search_messages(conn, "校验")) == 1


def test_topic_keeps_latest_summary_and_does_not_regress(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    base = dict(session_id="a" * 24, project_id="-d-Work-RepoA",
                source_kind="trae-memory")
    newer = catalog.upsert_topic(conn, TopicRecord(
        topic_time="2026-01-02 10:00:00", body="新摘要", **base))
    older = catalog.upsert_topic(conn, TopicRecord(
        topic_time="2026-01-01 10:00:00", body="旧摘要", **base))
    assert newer.session_created is True and older.session_created is False
    row = catalog.get_session(conn, "a" * 24)
    assert row["summary"] == "新摘要"
    assert row["updated_at"] == "2026-01-02 10:00:00"


def test_list_sessions_filters_by_project_and_date(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_topic(conn, TopicRecord(
        session_id="a" * 24, topic_time="2026-01-02 10:00:00", body="新摘要",
        project_id="-d-Work-RepoA", source_kind="trae-memory"))
    assert len(catalog.list_sessions(conn, project_id="-d-Work-RepoA")) == 1
    assert len(catalog.list_sessions(conn, project_id="-d-Work-RepoB")) == 0
    assert len(catalog.list_sessions(conn, date_from="2026-01-02")) == 1
    assert len(catalog.list_sessions(conn, date_to="2026-01-01")) == 0


def test_refresh_session_hashes_is_stable(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文一", "m1", 1))
    catalog.upsert_message(conn, _msg("正文二", "m2", 2))
    catalog.refresh_session_hashes(conn, {"a" * 24})
    first = catalog.get_session(conn, "a" * 24)["content_hash"]
    catalog.refresh_session_hashes(conn, {"a" * 24})
    assert first == catalog.get_session(conn, "a" * 24)["content_hash"]
    assert len(first) == 64


def test_stats_returns_all_counts(tmp_path: Path) -> None:
    conn = catalog.connect(tmp_path / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, _msg("正文"))
    assert catalog.stats(conn) == {
        "projects": 0, "sessions": 1, "messages": 1, "fts_rows": 1,
    }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_catalog.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.catalog'`

- [ ] **Step 3: 写实现**

```python
# sessionpack/catalog.py
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id      TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  local_path_hint TEXT,
  source_kind     TEXT NOT NULL,
  updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id  TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  title       TEXT,
  summary     TEXT,
  created_at  TEXT,
  updated_at  TEXT,
  content_hash TEXT,
  status      TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  ts         TEXT,
  intent     TEXT,
  outcome    TEXT,
  actions    TEXT,
  learned    TEXT,
  meta_json  TEXT,
  body       TEXT,
  PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS import_runs (
  run_id         TEXT PRIMARY KEY,
  adapter_id     TEXT NOT NULL,
  adapter_version INTEGER NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  result         TEXT NOT NULL,
  scanned_files  INTEGER NOT NULL DEFAULT 0,
  imported_rows  INTEGER NOT NULL DEFAULT 0,
  failed_rows    INTEGER NOT NULL DEFAULT 0,
  warnings_json  TEXT
);
CREATE TABLE IF NOT EXISTS import_files (
  path        TEXT PRIMARY KEY,
  size        INTEGER NOT NULL,
  mtime       REAL NOT NULL,
  offset      INTEGER NOT NULL DEFAULT 0,
  rows        INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  last_run_id TEXT,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


class WriteResult(NamedTuple):
    inserted: bool
    session_created: bool


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> str:
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
        "session_id UNINDEXED, message_id UNINDEXED, text, tokenize='trigram')"
    )
    conn.commit()
    return tokenizer_of(conn)


def tokenizer_of(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_fts'"
    ).fetchone()
    if row is None:
        return "none"
    return "trigram" if "trigram" in (row[0] or "") else "unicode61"


def upsert_project(conn: sqlite3.Connection, project: ProjectRecord) -> None:
    conn.execute(
        "INSERT INTO projects(project_id, display_name, local_path_hint, source_kind)"
        " VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
        " display_name=excluded.display_name, local_path_hint=excluded.local_path_hint",
        (project.project_id, project.display_name,
         project.local_path_hint, project.source_kind),
    )


def upsert_topic(conn: sqlite3.Connection, rec: TopicRecord) -> WriteResult:
    existing = conn.execute(
        "SELECT updated_at FROM sessions WHERE session_id=?", (rec.session_id,)
    ).fetchone()
    created = existing is None
    title = rec.body[:60] or None
    if created:
        conn.execute(
            "INSERT INTO sessions(session_id, project_id, source_kind, title, summary,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (rec.session_id, rec.project_id, rec.source_kind, title, rec.body,
             rec.topic_time, rec.topic_time),
        )
        return WriteResult(True, True)
    if rec.topic_time >= (existing["updated_at"] or ""):
        conn.execute(
            "UPDATE sessions SET project_id=?, title=?, summary=?, updated_at=?"
            " WHERE session_id=?",
            (rec.project_id, title, rec.body, rec.topic_time, rec.session_id),
        )
    return WriteResult(True, False)


def upsert_message(conn: sqlite3.Connection, rec: MessageRecord) -> WriteResult:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO messages(session_id, message_id, seq, ts, intent,"
        " outcome, actions, learned, meta_json, body) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rec.session_id, rec.message_id, rec.seq, rec.ts, rec.intent, rec.outcome,
         "|".join(rec.actions), "|".join(rec.learned),
         json.dumps(rec.meta, ensure_ascii=False) if rec.meta else None, rec.body),
    )
    if cursor.rowcount == 0:
        return WriteResult(False, False)
    conn.execute(
        "INSERT INTO message_fts(session_id, message_id, text) VALUES(?,?,?)",
        (rec.session_id, rec.message_id, rec.body),
    )
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (rec.session_id,)
    ).fetchone()
    created = existing is None
    if created:
        conn.execute(
            "INSERT INTO sessions(session_id, project_id, source_kind, title,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (rec.session_id, rec.project_id, rec.source_kind,
             (rec.intent or rec.body)[:60] or None, rec.ts, rec.ts),
        )
    else:
        conn.execute(
            "UPDATE sessions SET project_id=?,"
            " updated_at=MAX(COALESCE(updated_at,''), COALESCE(?, ''))"
            " WHERE session_id=?",
            (rec.project_id, rec.ts, rec.session_id),
        )
    return WriteResult(True, created)


def refresh_session_hashes(conn: sqlite3.Connection, session_ids: set[str]) -> None:
    for session_id in sorted(session_ids):
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM messages WHERE session_id=?"
                " ORDER BY seq, message_id",
                (session_id,),
            )
        ]
        digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE sessions SET content_hash=? WHERE session_id=?",
            (digest, session_id),
        )


def _fts_expr(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def search_messages(
    conn: sqlite3.Connection, term: str, limit: int = 50
) -> list[sqlite3.Row]:
    term = term.strip()
    if not term:
        return []
    if len(term) >= 3:
        try:
            return conn.execute(
                "SELECT m.session_id, m.message_id, m.body FROM message_fts"
                " JOIN messages m ON m.session_id=message_fts.session_id"
                " AND m.message_id=message_fts.message_id"
                " WHERE message_fts MATCH ? LIMIT ?",
                (_fts_expr(term), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    return conn.execute(
        "SELECT session_id, message_id, body FROM messages WHERE body LIKE ? LIMIT ?",
        (f"%{term}%", limit),
    ).fetchall()


def list_sessions(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    source_kind: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if project_id:
        clauses.append("s.project_id = ?")
        params.append(project_id)
    if source_kind:
        clauses.append("s.source_kind = ?")
        params.append(source_kind)
    day = "substr(COALESCE(s.updated_at, s.created_at, ''), 1, 10)"
    if date_from:
        clauses.append(f"{day} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{day} <= ?")
        params.append(date_to)
    if q:
        term = q.strip()
        if len(term) >= 3:
            clauses.append(
                "s.session_id IN (SELECT session_id FROM message_fts"
                " WHERE message_fts MATCH ?)"
            )
            params.append(_fts_expr(term))
        else:
            clauses.append(
                "(s.title LIKE ? OR s.summary LIKE ? OR s.session_id IN"
                " (SELECT session_id FROM messages WHERE body LIKE ?))"
            )
            params.extend([f"%{term}%"] * 3)
    sql = (
        "SELECT s.*, p.display_name FROM sessions s"
        " LEFT JOIN projects p ON p.project_id = s.project_id"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(s.updated_at, s.created_at, '') DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT s.*, p.display_name FROM sessions s"
        " LEFT JOIN projects p ON p.project_id = s.project_id"
        " WHERE s.session_id=?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def get_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY seq, message_id",
        (session_id,),
    )
    return [dict(row) for row in rows]


def get_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, COUNT(s.session_id) AS session_count FROM projects p"
        " LEFT JOIN sessions s ON s.project_id = p.project_id"
        " GROUP BY p.project_id ORDER BY p.display_name"
    )
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "fts_rows": conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0],
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_catalog.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/catalog.py tests/test_catalog.py
git commit -m "feat: 建立 catalog.sqlite 表结构与 FTS5 trigram 索引"
```

---

### Task 4: 批量导入器（跨文件提交、断点续传、幂等）

**Files:**
- Create: `sessionpack/mem.py`
- Create: `sessionpack/importer.py`
- Test: `tests/test_importer.py`

**Interfaces:**
- Consumes: `catalog.connect/init_schema/upsert_project/upsert_topic/upsert_message/refresh_session_hashes`、`TraeMemoryAdapter.enumerate/read_file`、`model.TopicRecord/MessageRecord`。
- Produces:
  - `mem.peak_rss_mb() -> float`
  - `importer.ImportStats`，字段 `run_id`、`files_declared`、`files_processed`、`files_skipped`、`rows_read`、`topics_applied`、`messages_inserted`、`rows_skipped`、`rows_failed`、`sessions_created`、`batches`、`elapsed_s`、`peak_rss_mb`、`stopped`、`failures`，含 `as_dict()`
  - `importer.import_source(adapter, root, catalog_path, *, batch=2000, reparse=False, project_filter=None, since=None, until=None, crash_after_rows=None, stop_after_rows=None) -> ImportStats`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_importer.py
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def _counts(path: Path) -> dict[str, int]:
    conn = catalog.connect(path)
    result = catalog.stats(conn)
    conn.close()
    return result


def test_import_populates_catalog(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    stats = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert stats.files_declared == 4
    assert stats.files_processed == 4
    assert stats.messages_inserted == 4
    assert stats.topics_applied == 2
    assert stats.sessions_created == 2
    assert stats.rows_failed == 2
    assert all(":" in failure for failure in stats.failures)
    assert not any(str(memory_tree) in failure for failure in stats.failures)
    assert _counts(db) == {
        "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
    }


def test_second_import_is_idempotent(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    again = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert again.files_skipped == 4
    assert again.messages_inserted == 0
    assert _counts(db)["messages"] == 4


def test_reparse_does_not_duplicate(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    again = import_source(TraeMemoryAdapter(), memory_tree, db, reparse=True)
    assert again.files_skipped == 0
    assert again.messages_inserted == 0
    assert again.rows_skipped == 4
    assert again.sessions_created == 0
    assert _counts(db)["messages"] == 4


def test_stop_then_resume_matches_full_import(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    partial = import_source(TraeMemoryAdapter(), memory_tree, db, stop_after_rows=2)
    assert partial.stopped is True
    assert partial.messages_inserted == 2
    rest = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert rest.stopped is False
    assert _counts(db) == {
        "projects": 2, "sessions": 2, "messages": 4, "fts_rows": 4,
    }


def test_appended_rows_are_imported_after_rewrite(
    memory_tree: Path, tmp_path: Path
) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    target = next(memory_tree.glob("projects/*/20260102/session_memory_*.jsonl"))
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"message_id": "m3", "message_summary_time": "2026-01-02 10:00:00",'
            ' "intent": "追加", "outcome": "新增一条", "actions": [], "learned": []}\n'
        )
    again = import_source(TraeMemoryAdapter(), memory_tree, db)
    assert again.messages_inserted == 1
    assert _counts(db)["messages"] == 5


def test_small_batch_still_idempotent(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    first = import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    assert first.batches >= 6
    second = import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    assert second.messages_inserted == 0
    assert _counts(db)["messages"] == 4


def test_project_and_date_filters(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    stats = import_source(
        TraeMemoryAdapter(), memory_tree, db,
        project_filter="-d-Work-RepoB", since="2026-01-02", until="2026-01-02",
    )
    assert stats.files_declared == 1
    assert _counts(db)["messages"] == 0
    assert _counts(db)["sessions"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_importer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.importer'`

- [ ] **Step 3: 写实现**

```python
# sessionpack/mem.py
from __future__ import annotations

import ctypes
import sys


def peak_rss_mb() -> float:
    if sys.platform == "win32":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return round(int(counters.PeakWorkingSetSize) / (1024 * 1024), 1)
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
```

```python
# sessionpack/importer.py
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters.base import MemoryAdapter
from sessionpack.adapters.trae_memory import project_display_name
from sessionpack.mem import peak_rss_mb
from sessionpack.model import MessageRecord, ProjectRecord, SourceFile, TopicRecord

BATCH_ROWS = 2000
MAX_FAILURES = 50


@dataclass
class ImportStats:
    run_id: str
    files_declared: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    rows_read: int = 0
    topics_applied: int = 0
    messages_inserted: int = 0
    rows_skipped: int = 0
    rows_failed: int = 0
    sessions_created: int = 0
    batches: int = 0
    elapsed_s: float = 0.0
    peak_rss_mb: float = 0.0
    stopped: bool = False
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "files_declared": self.files_declared,
            "files_processed": self.files_processed,
            "files_skipped": self.files_skipped,
            "rows_read": self.rows_read,
            "topics_applied": self.topics_applied,
            "messages_inserted": self.messages_inserted,
            "rows_skipped": self.rows_skipped,
            "rows_failed": self.rows_failed,
            "sessions_created": self.sessions_created,
            "batches": self.batches,
            "elapsed_s": self.elapsed_s,
            "peak_rss_mb": self.peak_rss_mb,
            "stopped": self.stopped,
            "failures": self.failures,
        }


@dataclass
class _FileState:
    rel: str
    offset: int = 0
    rows: int = 0
    done: bool = False
    dirty: bool = False
    error: str | None = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _selected(
    ref: SourceFile, project_filter: str | None, since: str | None, until: str | None
) -> bool:
    if project_filter and ref.project_id != project_filter:
        return False
    day = ref.path.parent.name
    if since and day < since.replace("-", ""):
        return False
    if until and day > until.replace("-", ""):
        return False
    return True


def import_source(
    adapter: MemoryAdapter,
    root: Path,
    catalog_path: Path,
    *,
    batch: int = BATCH_ROWS,
    reparse: bool = False,
    project_filter: str | None = None,
    since: str | None = None,
    until: str | None = None,
    crash_after_rows: int | None = None,
    stop_after_rows: int | None = None,
) -> ImportStats:
    started = time.perf_counter()
    root = Path(root)
    conn = catalog.connect(Path(catalog_path))
    catalog.init_schema(conn)
    stats = ImportStats(run_id=uuid.uuid4().hex)
    conn.execute(
        "INSERT INTO import_runs(run_id, adapter_id, adapter_version, started_at, result)"
        " VALUES(?,?,?,?,?)",
        (stats.run_id, adapter.id, adapter.version, _now(), "running"),
    )
    conn.commit()

    files = [
        ref for ref in adapter.enumerate(root)
        if _selected(ref, project_filter, since, until)
    ]
    stats.files_declared = len(files)

    projects: dict[str, ProjectRecord] = {}
    for ref in files:
        projects[ref.project_id] = ProjectRecord(
            project_id=ref.project_id,
            display_name=project_display_name(ref.project_id),
            local_path_hint=ref.raw_project_dir,
            source_kind=adapter.source_kind,
        )
    for project in projects.values():
        catalog.upsert_project(conn, project)

    existing = {
        row["path"]: row for row in conn.execute("SELECT * FROM import_files")
    }
    states: dict[str, _FileState] = {}
    to_insert: list[tuple[str, int, float]] = []
    resets: list[tuple[int, float, str]] = []
    for ref in files:
        key = str(ref.path)
        try:
            info = ref.path.stat()
        except OSError as exc:
            stats.rows_failed += 1
            stats.failures.append(f"{_relative(ref.path, root)}: {str(exc)[:60]}")
            continue
        row = existing.get(key)
        if row is None:
            to_insert.append((key, info.st_size, info.st_mtime))
            states[key] = _FileState(rel=_relative(ref.path, root))
            continue
        same = (
            row["size"] == info.st_size
            and abs(row["mtime"] - info.st_mtime) < 1e-6
        )
        states[key] = _FileState(
            rel=_relative(ref.path, root),
            offset=row["offset"],
            rows=row["rows"],
            done=bool(row["done"]),
            error=row["error"],
        )
        if not same and not reparse:
            states[key].offset = 0
            states[key].rows = 0
            states[key].done = False
            resets.append((info.st_size, info.st_mtime, key))

    if to_insert:
        conn.executemany(
            "INSERT OR IGNORE INTO import_files(path, size, mtime) VALUES(?,?,?)",
            to_insert,
        )
    for size, mtime, key in resets:
        conn.execute(
            "UPDATE import_files SET size=?, mtime=?, offset=0, rows=0, done=0,"
            " error=NULL WHERE path=?",
            (size, mtime, key),
        )
    conn.commit()

    dirty: set[str] = set()
    uncommitted = 0

    def commit(force: bool = False) -> None:
        nonlocal uncommitted
        if not uncommitted and not force:
            return
        for key in list(dirty):
            state = states[key]
            conn.execute(
                "UPDATE import_files SET offset=?, rows=?, done=?, last_run_id=?,"
                " error=? WHERE path=?",
                (state.offset, state.rows, 1 if state.done else 0,
                 stats.run_id, state.error, key),
            )
        dirty.clear()
        conn.commit()
        if uncommitted:
            stats.batches += 1
        uncommitted = 0

    touched: set[str] = set()
    stop = False
    for ref in files:
        key = str(ref.path)
        state = states.get(key)
        if state is None:
            continue
        if state.done and not reparse:
            stats.files_skipped += 1
            continue
        start_offset, start_seq = (0, 0) if reparse else (state.offset, state.rows)
        try:
            for offset, record, error in adapter.read_file(
                ref, start_offset, start_seq
            ):
                if error:
                    stats.rows_failed += 1
                    state.error = error
                    if len(stats.failures) < MAX_FAILURES:
                        stats.failures.append(f"{state.rel}: {error}")
                    continue
                if record is None:
                    continue
                if isinstance(record, TopicRecord):
                    result = catalog.upsert_topic(conn, record)
                    stats.topics_applied += 1
                else:
                    result = catalog.upsert_message(conn, record)
                    if result.inserted:
                        stats.messages_inserted += 1
                    else:
                        stats.rows_skipped += 1
                    state.rows += 1
                if result.session_created:
                    stats.sessions_created += 1
                touched.add(record.session_id)
                stats.rows_read += 1
                state.offset = offset
                state.dirty = True
                dirty.add(key)
                uncommitted += 1
                if crash_after_rows is not None and stats.rows_read >= crash_after_rows:
                    os._exit(9)
                if stop_after_rows is not None and stats.rows_read >= stop_after_rows:
                    stop = True
                    break
                if uncommitted >= batch:
                    commit()
        except OSError as exc:
            stats.rows_failed += 1
            state.error = str(exc)[:60]
            state.done = False
            state.dirty = True
            dirty.add(key)
            if len(stats.failures) < MAX_FAILURES:
                stats.failures.append(f"{state.rel}: {str(exc)[:60]}")
        else:
            if stop:
                stats.stopped = True
                commit()
                break
            state.done = True
            state.dirty = True
        dirty.add(key)
        stats.files_processed += 1

    commit()
    if touched:
        catalog.refresh_session_hashes(conn, touched)
    stats.elapsed_s = round(time.perf_counter() - started, 2)
    stats.peak_rss_mb = peak_rss_mb()
    conn.execute(
        "UPDATE import_runs SET finished_at=?, result=?, scanned_files=?,"
        " imported_rows=?, failed_rows=?, warnings_json=? WHERE run_id=?",
        (
            _now(),
            "stopped" if stop else "ok",
            stats.files_declared,
            stats.messages_inserted + stats.topics_applied,
            stats.rows_failed,
            json.dumps(
                {"failures": stats.failures, "tokenizer": catalog.tokenizer_of(conn)},
                ensure_ascii=False,
            ),
            stats.run_id,
        ),
    )
    conn.commit()
    conn.close()
    return stats
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_importer.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/mem.py sessionpack/importer.py tests/test_importer.py
git commit -m "feat: 实现跨文件批量提交与断点续传的导入器"
```

---

### Task 5: 归档包构建、校验与导入

**Files:**
- Create: `sessionpack/archive.py`
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: `catalog.connect/init_schema/upsert_project/upsert_topic/upsert_message/get_messages`、`sessionpack.__version__`。
- Produces:
  - `archive.FORMAT_NAME = "sessionpack-archive"`、`FORMAT_VERSION = 1`、`MANIFEST_NAME = "manifest.json"`
  - `archive.ArchiveResult(path, archive_id, project_id, sessions, messages, bytes)`
  - `archive.VerifyIssue(code, detail)`、`archive.VerifyResult(path, ok, issues, manifest)`
  - `archive.ArchiveImportResult(archive_id, sessions, messages, sessions_created, messages_inserted)`
  - `archive.ArchiveError(issues)`
  - `archive.compute_archive_id(files: list[dict]) -> str`
  - `archive.build_archive(conn, out_dir: Path, project_id: str, *, now: str | None = None) -> ArchiveResult`
  - `archive.verify_archive(path: Path) -> VerifyResult`
  - `archive.import_archive(conn, path: Path) -> ArchiveImportResult`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_archive.py
import json
import zipfile
from pathlib import Path

import pytest

from sessionpack import archive, catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


@pytest.fixture
def filled_db(memory_tree: Path, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return db


def _rewrite(src: Path, dst: Path, mutate: dict[str, bytes] | None = None,
             drop: set[str] | None = None, add: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            if drop and name in drop:
                continue
            data = mutate[name] if mutate and name in mutate else zin.read(name)
            zout.writestr(name, data)
        for name, data in (add or {}).items():
            zout.writestr(name, data)


def test_build_archive_has_manifest_and_is_stable(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    first = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA",
                                  now="2026-01-03T00:00:00")
    second = archive.build_archive(conn, tmp_path / "b", "-d-Work-RepoA",
                                   now="2026-01-03T00:00:00")
    assert first.archive_id == second.archive_id
    assert first.sessions == 1 and first.messages == 2
    with zipfile.ZipFile(first.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["archive_id"] == first.archive_id
    assert manifest["format"] == "sessionpack-archive"
    assert len(manifest["files"]) == 2
    assert archive.verify_archive(first.path).ok is True


def test_verify_detects_hash_mismatch(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"sessions/" + "a" * 24 + "/messages.ndjson": b"x"})
    result = archive.verify_archive(bad)
    assert result.ok is False
    assert "E_HASH_MISMATCH" in {issue.code for issue in result.issues}


def test_verify_detects_missing_manifest(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, drop={"manifest.json"})
    result = archive.verify_archive(bad)
    assert [issue.code for issue in result.issues] == ["E_MANIFEST_MISSING"]


def test_verify_detects_unregistered_file(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, add={"extra.txt": b"hello"})
    codes = {issue.code for issue in archive.verify_archive(bad).issues}
    assert "E_UNREGISTERED_FILE" in codes


def test_verify_rejects_path_traversal(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    with zipfile.ZipFile(built.path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["files"].append({"path": "../evil.txt", "sha256": "0" * 64, "size": 0})
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad,
             mutate={"manifest.json": json.dumps(manifest).encode("utf-8")},
             add={"../evil.txt": b"x"})
    result = archive.verify_archive(bad)
    assert "E_PATH_ILLEGAL" in {issue.code for issue in result.issues}


def test_import_archive_roundtrip(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    fresh_db = tmp_path / "fresh.sqlite"
    fresh = catalog.connect(fresh_db)
    catalog.init_schema(fresh)
    result = archive.import_archive(fresh, built.path)
    assert result.sessions == 1 and result.messages == 2
    assert result.messages_inserted == 2
    again = archive.import_archive(fresh, built.path)
    assert again.messages_inserted == 0
    assert catalog.stats(fresh)["messages"] == 2


def test_import_refuses_tampered_archive(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    built = archive.build_archive(conn, tmp_path / "a", "-d-Work-RepoA")
    bad = tmp_path / "bad.zip"
    _rewrite(built.path, bad, mutate={"sessions/" + "a" * 24 + "/messages.ndjson": b"x"})
    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    with pytest.raises(archive.ArchiveError):
        archive.import_archive(fresh, bad)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_archive.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.archive'`

- [ ] **Step 3: 写实现**

```python
# sessionpack/archive.py
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sessionpack import __version__, catalog
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord

FORMAT_NAME = "sessionpack-archive"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    archive_id: str
    project_id: str
    sessions: int
    messages: int
    bytes: int


@dataclass(frozen=True)
class VerifyIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class VerifyResult:
    path: Path
    ok: bool
    issues: tuple[VerifyIssue, ...]
    manifest: dict | None


@dataclass(frozen=True)
class ArchiveImportResult:
    archive_id: str
    sessions: int
    messages: int
    sessions_created: int
    messages_inserted: int


class ArchiveError(RuntimeError):
    def __init__(self, issues: tuple[VerifyIssue, ...] | list[VerifyIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.code}: {i.detail}" for i in self.issues))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _slug(text: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in text)
    cleaned = cleaned.strip("-")[:60]
    return cleaned or "project"


def _unsafe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return True
    pure = PurePosixPath(name)
    return pure.is_absolute() or ".." in pure.parts


def compute_archive_id(files: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(files, key=lambda item: item["path"]):
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_archive(
    conn: sqlite3.Connection,
    out_dir: Path,
    project_id: str,
    *,
    now: str | None = None,
) -> ArchiveResult:
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE project_id=? ORDER BY session_id", (project_id,)
    ).fetchall()
    if not sessions:
        raise ValueError(f"项目没有可归档的会话: {project_id}")
    project_row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    created_at = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=out_dir))
    try:
        entries: list[dict] = []
        total_messages = 0
        for session in sessions:
            session_id = session["session_id"]
            messages = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY seq, message_id",
                (session_id,),
            ).fetchall()
            total_messages += len(messages)
            session_dir = work / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "session_id": session_id,
                "project_id": session["project_id"],
                "source_kind": session["source_kind"],
                "title": session["title"],
                "summary": session["summary"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "content_hash": session["content_hash"],
                "status": session["status"],
            }
            meta_path = session_dir / "meta.json"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            messages_path = session_dir / "messages.ndjson"
            with messages_path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in messages:
                    payload = {
                        "message_id": row["message_id"],
                        "seq": row["seq"],
                        "ts": row["ts"],
                        "intent": row["intent"],
                        "outcome": row["outcome"],
                        "actions": [x for x in (row["actions"] or "").split("|") if x],
                        "learned": [x for x in (row["learned"] or "").split("|") if x],
                        "meta": json.loads(row["meta_json"]) if row["meta_json"] else None,
                        "body": row["body"] or "",
                    }
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            for path in (meta_path, messages_path):
                entries.append({
                    "path": path.relative_to(work).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                })
        archive_id = compute_archive_id(entries)
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "archive_id": archive_id,
            "created_at": created_at,
            "project": {
                "project_id": project_id,
                "display_name": (
                    project_row["display_name"] if project_row is not None else project_id
                ),
                "source_kind": sessions[0]["source_kind"],
            },
            "counts": {"sessions": len(sessions), "messages": total_messages},
            "producer": {"name": "sessionpack", "version": __version__},
            "files": sorted(entries, key=lambda item: item["path"]),
        }
        (work / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        temp_zip = out_dir / f".tmp-{archive_id[:12]}.zip"
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
            for entry in manifest["files"]:
                bundle.write(work / entry["path"], entry["path"])
            bundle.write(work / MANIFEST_NAME, MANIFEST_NAME)
        check = verify_archive(temp_zip)
        if not check.ok:
            temp_zip.unlink(missing_ok=True)
            raise ArchiveError(check.issues)
        final = out_dir / f"{_slug(project_id)}-{created_at[:10]}-{archive_id[:12]}.zip"
        try:
            os.replace(temp_zip, final)
        except OSError:
            temp_zip.unlink(missing_ok=True)
            raise
        return ArchiveResult(
            path=final,
            archive_id=archive_id,
            project_id=project_id,
            sessions=len(sessions),
            messages=total_messages,
            bytes=final.stat().st_size,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify_archive(path: Path) -> VerifyResult:
    path = Path(path)
    try:
        bundle = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return VerifyResult(path, False,
                            (VerifyIssue("E_ZIP_UNREADABLE", str(exc)[:120]),), None)
    issues: list[VerifyIssue] = []
    with bundle:
        names = bundle.namelist()
        if MANIFEST_NAME not in names:
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_MISSING", "缺少 manifest.json"),),
                                None)
        try:
            manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_INVALID", str(exc)[:120]),), None)
        if not isinstance(manifest, dict):
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_INVALID", "manifest 顶层必须是对象"),),
                                None)
        if manifest.get("format") != FORMAT_NAME:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "format 不匹配"))
        if manifest.get("format_version") != FORMAT_VERSION:
            issues.append(VerifyIssue("E_FORMAT_VERSION", "format_version 不支持"))
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "files 为空"))
            files = []
        seen: set[str] = set()
        for entry in files:
            name = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(name, str) or _unsafe_member(name):
                issues.append(VerifyIssue("E_PATH_ILLEGAL", repr(name)))
                continue
            if name in seen:
                issues.append(VerifyIssue("E_DUPLICATE_PATH", name))
                continue
            seen.add(name)
            if name not in names:
                issues.append(VerifyIssue("E_FILE_MISSING", name))
                continue
            with bundle.open(name) as handle:
                digest = _sha256_stream(handle)
            if digest != entry.get("sha256"):
                issues.append(VerifyIssue("E_HASH_MISMATCH", name))
        for name in sorted(set(names) - seen - {MANIFEST_NAME}):
            issues.append(VerifyIssue("E_UNREGISTERED_FILE", name))
        if not issues and manifest.get("archive_id") != compute_archive_id(files):
            issues.append(VerifyIssue("E_ARCHIVE_ID_MISMATCH", "archive_id 不可复现"))
    return VerifyResult(path, not issues, tuple(issues), manifest)


def import_archive(conn: sqlite3.Connection, path: Path) -> ArchiveImportResult:
    path = Path(path)
    result = verify_archive(path)
    if not result.ok or result.manifest is None:
        raise ArchiveError(result.issues)
    manifest = result.manifest
    project = manifest["project"]
    catalog.upsert_project(conn, ProjectRecord(
        project_id=project["project_id"],
        display_name=project.get("display_name") or project["project_id"],
        local_path_hint=None,
        source_kind=project.get("source_kind") or "archive",
    ))
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO import_runs(run_id, adapter_id, adapter_version, started_at, result)"
        " VALUES(?,?,?,?,?)",
        (run_id, f"archive:{manifest['archive_id']}", manifest["format_version"],
         datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "running"),
    )
    sessions = messages = created = inserted = 0
    touched: set[str] = set()
    with zipfile.ZipFile(path) as bundle:
        for entry in manifest["files"]:
            name = entry["path"]
            if not name.endswith("/meta.json"):
                continue
            session_dir = name.rsplit("/", 1)[0]
            meta = json.loads(bundle.read(name).decode("utf-8"))
            session_id = meta["session_id"]
            touched.add(session_id)
            summary = meta.get("summary") or meta.get("title") or ""
            if summary:
                outcome = catalog.upsert_topic(conn, TopicRecord(
                    session_id=session_id,
                    topic_time=meta.get("updated_at") or meta.get("created_at") or "",
                    body=summary,
                    project_id=meta["project_id"],
                    source_kind=meta.get("source_kind") or "archive",
                ))
                created += int(outcome.session_created)
            sessions += 1
            rows = bundle.read(f"{session_dir}/messages.ndjson").decode("utf-8")
            for line in rows.splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                outcome = catalog.upsert_message(conn, MessageRecord(
                    session_id=session_id,
                    message_id=payload["message_id"],
                    seq=int(payload.get("seq") or 0),
                    ts=payload.get("ts"),
                    intent=payload.get("intent"),
                    outcome=payload.get("outcome"),
                    actions=tuple(payload.get("actions") or ()),
                    learned=tuple(payload.get("learned") or ()),
                    meta=payload.get("meta"),
                    body=payload.get("body") or "",
                    project_id=meta["project_id"],
                    source_kind=meta.get("source_kind") or "archive",
                ))
                inserted += int(outcome.inserted)
                created += int(outcome.session_created)
                messages += 1
    if touched:
        catalog.refresh_session_hashes(conn, touched)
    conn.execute(
        "UPDATE import_runs SET finished_at=?, result=?, scanned_files=?,"
        " imported_rows=?, failed_rows=? WHERE run_id=?",
        (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "ok",
         len(manifest["files"]), inserted, 0, run_id),
    )
    conn.commit()
    return ArchiveImportResult(
        archive_id=manifest["archive_id"],
        sessions=sessions,
        messages=messages,
        sessions_created=created,
        messages_inserted=inserted,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_archive.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/archive.py tests/test_archive.py
git commit -m "feat: 实现 zip 加 manifest 归档的构建、校验与导入"
```

---

### Task 6: 确定性续接摘要与 Markdown/JSON 导出

**Files:**
- Create: `sessionpack/summary.py`
- Create: `sessionpack/export.py`
- Test: `tests/test_summary.py`

**Interfaces:**
- Consumes: `catalog.connect/get_session/get_messages/list_sessions`。
- Produces:
  - `summary.split_list(value: str | None) -> list[str]`
  - `summary.extract_file_mentions(texts: list[str]) -> list[str]`
  - `summary.build_summary(session: dict, messages: list[dict]) -> dict`
  - `summary.render_markdown(session: dict, messages: list[dict]) -> str`
  - `export.ExportResult(paths: tuple[Path, ...], sessions: int, messages: int)`
  - `export.export_session(conn, session_id: str, dest_dir: Path, fmt: str = "markdown") -> Path`
  - `export.export_project(conn, project_id: str, dest_dir: Path, fmt: str = "markdown") -> ExportResult`
  - `export.export_all(conn, dest_dir: Path, fmt: str = "markdown") -> ExportResult`

摘要必须确定性：同样的输入产生逐字节相同的输出，不包含当前时间、随机数或绝对路径。字段顺序固定，去重保留首次出现顺序，文件列表排序后输出。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_summary.py
import json
from pathlib import Path

import pytest

from sessionpack import catalog, export, summary
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


@pytest.fixture
def filled_db(memory_tree: Path, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return db


def _session(conn, session_id: str = "a" * 24):
    return catalog.get_session(conn, session_id), catalog.get_messages(conn, session_id)


def test_summary_is_deterministic_and_has_sections(filled_db: Path) -> None:
    conn = catalog.connect(filled_db)
    session, messages = _session(conn)
    first = summary.render_markdown(session, messages)
    second = summary.render_markdown(session, messages)
    assert first == second
    assert "## 关键结论" in first
    assert "## 涉及文件" in first
    assert "批量提交比逐文件提交快" in first
    assert str(filled_db) not in first


def test_summary_dedupes_learned_and_sorts_files() -> None:
    session = {
        "session_id": "a" * 24,
        "project_id": "-d-Work-RepoA",
        "display_name": "-d-Work-RepoA",
        "title": "测试会话",
        "summary": "测试摘要",
        "created_at": "2026-01-01 10:00:00",
        "updated_at": "2026-01-01 10:10:00",
    }
    messages = [
        {"session_id": "a" * 24, "message_id": "m1", "seq": 1,
         "ts": "2026-01-01 10:00:00", "intent": "改界面", "outcome": "完成",
         "actions": "改 app.js|改 app.js", "learned": "结论甲|结论乙", "body": ""},
        {"session_id": "a" * 24, "message_id": "m2", "seq": 2,
         "ts": "2026-01-01 10:10:00", "intent": "补测试", "outcome": "通过",
         "actions": "改 src/a.py", "learned": "结论甲", "body": ""},
    ]
    data = summary.build_summary(session, messages)
    assert data["learned"] == ["结论甲", "结论乙"]
    assert data["actions"] == ["改 app.js", "改 src/a.py"]
    assert data["files"] == ["app.js", "src/a.py"]
    assert data["latest_outcome"] == "通过"


def test_extract_file_mentions_strips_absolute_roots() -> None:
    texts = [
        r"改 E:\Workplace\SessionPack\sessionpack\summary.py",
        "改 /home/dev/repo/main.py",
        r"改 D:\repo\src\a.py 和 ./tools/b.py",
    ]
    assert summary.extract_file_mentions(texts) == [
        "a.py",
        "main.py",
        "summary.py",
        "tools/b.py",
    ]


def test_export_session_writes_markdown_and_json(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    out = tmp_path / "out"
    md = export.export_session(conn, "a" * 24, out, "markdown")
    js = export.export_session(conn, "a" * 24, out, "json")
    assert md.name == "a" * 24 + ".md"
    assert "实现归档包的校验流程" in md.read_text(encoding="utf-8")
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["summary"]["session_id"] == "a" * 24
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["actions"] == ["读取 topics.md"]
    assert list(out.glob("*.tmp")) == []


def test_export_all_covers_every_session(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    result = export.export_all(conn, tmp_path / "all")
    assert result.sessions == 2
    assert result.messages == 4
    assert len(result.paths) == 2
    assert all(path.is_file() for path in result.paths)


def test_export_unknown_session_raises(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    with pytest.raises(KeyError):
        export.export_session(conn, "f" * 24, tmp_path / "out")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_summary.py -v`
Expected: FAIL，`ImportError: cannot import name 'export' from 'sessionpack'`（`from sessionpack import catalog, export, summary` 按名字表解析，`export` 先于 `summary` 触发；模块缺失即可）

- [ ] **Step 3: 写实现**

```python
# sessionpack/summary.py
from __future__ import annotations

import re

FILE_RE = re.compile(
    r"(?:[A-Za-z]:[/\\])?[A-Za-z0-9_./\\-]*[A-Za-z0-9_-]+\.(?:py|ts|tsx|js|jsx|json|md|sql|toml|ya?ml|css|html)\b",
    re.IGNORECASE,
)

ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\])")


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split("|") if part]


def extract_file_mentions(texts: list[str]) -> list[str]:
    found: set[str] = set()
    for text in texts:
        for match in FILE_RE.findall(text or ""):
            normalized = match.replace("\\", "/")
            if ABSOLUTE_RE.match(match):
                normalized = normalized.rsplit("/", 1)[-1]
            while normalized.startswith("./"):
                normalized = normalized[2:]
            found.add(normalized)
    return sorted(found)


def build_summary(session: dict, messages: list[dict]) -> dict:
    learned: list[str] = []
    actions: list[str] = []
    latest_outcome = ""
    for message in messages:
        for item in split_list(message.get("learned")):
            if item not in learned:
                learned.append(item)
        for item in split_list(message.get("actions")):
            if item not in actions:
                actions.append(item)
        if message.get("outcome"):
            latest_outcome = message["outcome"]
    files = extract_file_mentions(actions + learned + [latest_outcome])
    return {
        "session_id": session["session_id"],
        "project_id": session["project_id"],
        "project_name": session.get("display_name") or session["project_id"],
        "title": session.get("title") or "（无标题）",
        "topic": session.get("summary") or "",
        "started_at": session.get("created_at") or "",
        "updated_at": session.get("updated_at") or "",
        "message_count": len(messages),
        "latest_outcome": latest_outcome,
        "learned": learned,
        "actions": actions,
        "files": files,
    }


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- （无记录）"]


def render_markdown(session: dict, messages: list[dict]) -> str:
    data = build_summary(session, messages)
    lines = [
        f"# 续接摘要：{data['title']}",
        "",
        f"- 项目：{data['project_name']}（{data['project_id']}）",
        f"- 会话：{data['session_id']}",
        f"- 更新时间：{data['updated_at']}",
        f"- 消息：{data['message_count']} 条",
        "",
        "## 主题",
        "",
        data["topic"] or "（无主题摘要）",
        "",
        "## 最新进展",
        "",
        data["latest_outcome"] or "（无记录）",
        "",
        "## 关键结论",
        "",
        *_bullets(data["learned"]),
        "",
        "## 已执行动作",
        "",
        *_bullets(data["actions"]),
        "",
        "## 涉及文件",
        "",
        *_bullets([f"`{path}`" for path in data["files"]]),
        "",
    ]
    return "\n".join(lines)
```

```python
# sessionpack/export.py
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sessionpack import catalog
from sessionpack.summary import build_summary, render_markdown, split_list


@dataclass(frozen=True)
class ExportResult:
    paths: tuple[Path, ...]
    sessions: int
    messages: int


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _message_payload(message: dict) -> dict:
    return {
        "message_id": message["message_id"],
        "seq": message["seq"],
        "ts": message.get("ts"),
        "intent": message.get("intent"),
        "outcome": message.get("outcome"),
        "actions": split_list(message.get("actions")),
        "learned": split_list(message.get("learned")),
        "body": message.get("body") or "",
    }


def _render(session: dict, messages: list[dict], fmt: str) -> str:
    if fmt == "json":
        payload = {
            "summary": build_summary(session, messages),
            "messages": [_message_payload(message) for message in messages],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if fmt == "markdown":
        return render_markdown(session, messages)
    raise ValueError(f"不支持的导出格式: {fmt}")


def export_session(
    conn: sqlite3.Connection, session_id: str, dest_dir: Path, fmt: str = "markdown"
) -> Path:
    session = catalog.get_session(conn, session_id)
    if session is None:
        raise KeyError(session_id)
    messages = catalog.get_messages(conn, session_id)
    suffix = "json" if fmt == "json" else "md"
    path = Path(dest_dir) / f"{session_id}.{suffix}"
    _atomic_write(path, _render(session, messages, fmt))
    return path


def _export_rows(conn: sqlite3.Connection, rows: list[dict], dest_dir: Path,
                 fmt: str) -> ExportResult:
    paths: list[Path] = []
    messages = 0
    for row in rows:
        paths.append(export_session(conn, row["session_id"], dest_dir, fmt))
        messages += len(catalog.get_messages(conn, row["session_id"]))
    return ExportResult(tuple(paths), len(paths), messages)


def export_project(
    conn: sqlite3.Connection, project_id: str, dest_dir: Path, fmt: str = "markdown"
) -> ExportResult:
    rows = catalog.list_sessions(conn, project_id=project_id, limit=100000)
    return _export_rows(conn, rows, dest_dir, fmt)


def export_all(
    conn: sqlite3.Connection, dest_dir: Path, fmt: str = "markdown"
) -> ExportResult:
    rows = catalog.list_sessions(conn, limit=100000)
    return _export_rows(conn, rows, dest_dir, fmt)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_summary.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/summary.py sessionpack/export.py tests/test_summary.py
git commit -m "feat: 实现确定性续接摘要与 Markdown/JSON 导出"
```

---

### Task 7: CLI 六命令、读取边界审计与诊断

**Files:**
- Create: `sessionpack/audit.py`
- Create: `sessionpack/diagnostics.py`
- Create: `sessionpack/cli.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `paths.ensure_layout/resolve_data_dir/default_memory_root`、`adapters.get_adapter`、`catalog.connect/init_schema/stats/tokenizer_of`、`importer.import_source`、`archive.import_archive/verify_archive`、`export.export_session/export_project/export_all`、`diagnostics.collect`。
- Produces:
  - `audit.ReadBoundaryError(unexpected: list[str])`
  - `audit.ReadAudit(root: Path, allowed: list[Path])`，上下文管理器；`opened` 为已读取的白名单路径集合，`check(declared) -> None` 在越界时抛 `ReadBoundaryError`
  - `diagnostics.private_db_path() -> Path`
  - `diagnostics.collect(memory_root: Path, layout: Layout) -> dict`
  - `cli.build_parser() -> argparse.ArgumentParser`
  - `cli.main(argv: list[str] | None = None) -> int`
  - 退出码常量：`EXIT_OK = 0`、`EXIT_ERROR = 1`、`EXIT_USAGE = 2`、`EXIT_VERIFY = 3`

`serve` 在 Task 8 才实现。`_cmd_serve` 内使用局部 `from sessionpack.server import serve`，保证本任务的测试不依赖 Task 8 的文件。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli.py
import json
from pathlib import Path

from sessionpack import catalog
from sessionpack.cli import main


def test_doctor_reports_adapter(memory_tree: Path, tmp_path: Path, capsys) -> None:
    code = main(["doctor", "--memory-root", str(memory_tree),
                 "--data-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "trae-cn-memory" in out
    assert "记忆目录" in out
    assert "不读取" in out


def test_scan_json_reports_projects(memory_tree: Path, capsys) -> None:
    code = main(["scan", "--memory-root", str(memory_tree), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["projects"] == 2
    assert data["topic_files"] == 2
    assert data["jsonl_files"] == 2


def test_scan_missing_root_returns_two(tmp_path: Path, capsys) -> None:
    code = main(["scan", "--memory-root", str(tmp_path / "nope")])
    assert code == 2
    assert "不存在" in capsys.readouterr().err


def test_import_creates_catalog(memory_tree: Path, tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    code = main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_dir)])
    assert code == 0
    conn = catalog.connect(data_dir / "catalog.sqlite")
    stats = catalog.stats(conn)
    conn.close()
    assert stats["messages"] == 4
    assert "消息" in capsys.readouterr().out


def test_import_with_audit_passes_and_reports(memory_tree: Path, tmp_path: Path,
                                              capsys) -> None:
    code = main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(tmp_path / "data"), "--audit"])
    assert code == 0
    assert "读取边界" in capsys.readouterr().out


def test_export_session_from_cli(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "export"
    code = main(["export", "--data-dir", str(data_dir),
                 "--session", "a" * 24, "--out", str(out)])
    assert code == 0
    assert (out / ("a" * 24 + ".md")).is_file()


def test_export_all_json_from_cli(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "export"
    code = main(["export", "--data-dir", str(data_dir), "--all",
                 "--format", "json", "--out", str(out)])
    assert code == 0
    assert len(list(out.glob("*.json"))) == 2


def test_export_archive_creates_zip(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "archives"
    code = main(["export", "--data-dir", str(data_dir), "--all",
                 "--archive", "--out", str(out)])
    assert code == 0
    assert len(list(out.glob("*.zip"))) == 2


def test_verify_bad_archive_returns_three(tmp_path: Path, capsys) -> None:
    code = main(["verify", str(tmp_path / "nope.zip")])
    assert code == 3
    assert "E_ZIP_UNREADABLE" in capsys.readouterr().out


def test_serve_without_catalog_returns_two(tmp_path: Path, capsys) -> None:
    code = main(["serve", "--data-dir", str(tmp_path / "empty")])
    assert code == 2
    assert "请先运行" in capsys.readouterr().err
```

```python
# tests/test_audit.py
from pathlib import Path

import pytest

from sessionpack.audit import ReadAudit, ReadBoundaryError
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def test_audit_records_only_declared_files(memory_tree: Path, tmp_path: Path) -> None:
    adapter = TraeMemoryAdapter()
    declared = adapter.declared_paths(memory_tree)
    with ReadAudit(memory_tree, declared) as audit:
        import_source(adapter, memory_tree, tmp_path / "catalog.sqlite")
    audit.check(declared)
    assert len(audit.opened) == 4


def test_audit_flags_undeclared_open(memory_tree: Path) -> None:
    token = memory_tree / "trae-jwt-token"
    with ReadAudit(memory_tree, []) as audit:
        token.read_text(encoding="utf-8")
    with pytest.raises(ReadBoundaryError) as exc:
        audit.check([])
    assert "trae-jwt-token" in str(exc.value)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_cli.py tests/test_audit.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.audit'`

- [ ] **Step 3: 写实现**

```python
# sessionpack/audit.py
from __future__ import annotations

import sys
from pathlib import Path


class ReadBoundaryError(RuntimeError):
    def __init__(self, unexpected: list[str]) -> None:
        self.unexpected = tuple(sorted(unexpected))
        super().__init__("读取白名单外的文件: " + ", ".join(self.unexpected))


class ReadAudit:
    """基于 sys.addaudithook 的读取边界审计。

    审计钩子一旦安装无法卸载，因此只记录 root 之下的打开事件，
    其他路径（索引库、pytest 自身文件）不参与判定。
    """

    def __init__(self, root: Path, allowed: list[Path]) -> None:
        self.root = Path(root).resolve()
        self.allowed = {str(Path(path).resolve()).casefold() for path in allowed}
        self.opened: set[str] = set()
        self.violations: list[str] = []

    def __enter__(self) -> "ReadAudit":
        sys.addaudithook(self._hook)
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def _hook(self, event: str, args: tuple) -> None:
        if event != "open" or not args:
            return
        raw = args[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if not isinstance(raw, str):
            return
        try:
            resolved = Path(raw).resolve()
        except (OSError, ValueError):
            return
        if resolved != self.root and self.root not in resolved.parents:
            return
        key = str(resolved).casefold()
        if key in self.allowed:
            self.opened.add(key)
        else:
            self.violations.append(resolved.relative_to(self.root).as_posix())

    def check(self, declared: list[Path]) -> None:
        allowed = {str(Path(path).resolve()).casefold() for path in declared}
        unknown = self.opened - allowed
        if self.violations or unknown:
            raise ReadBoundaryError(self.violations + sorted(unknown))


def audit_summary(audit: ReadAudit, declared: list[Path]) -> dict:
    return {
        "declared": len(declared),
        "opened": len(audit.opened),
        "violations": list(audit.violations),
    }
```

```python
# sessionpack/diagnostics.py
from __future__ import annotations

import os
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.paths import Layout


def private_db_path() -> Path:
    base = os.environ.get("APPDATA")
    roaming = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return roaming / "Trae CN" / "ModularData" / "ai-agent" / "database.db"


def collect(memory_root: Path, layout: Layout) -> dict:
    adapter = TraeMemoryAdapter()
    exists = Path(memory_root).is_dir()
    info = adapter.inspect(memory_root) if exists else None
    database = private_db_path()
    result: dict = {
        "memory_root": str(memory_root),
        "memory_root_exists": exists,
        "adapter": {
            "id": adapter.id,
            "version": adapter.version,
            "source_kind": adapter.source_kind,
            "detected": adapter.detect(memory_root) if exists else False,
        },
        "source": info.as_dict() if info else None,
        "private_db": {
            "path": str(database),
            "exists": database.is_file(),
            "size": database.stat().st_size if database.is_file() else 0,
            "readable": False,
            "note": "加密私有库，不在读取白名单内，仅报告存在性与体积",
        },
        "catalog": {
            "path": str(layout.catalog),
            "exists": layout.catalog.is_file(),
            "stats": {"projects": 0, "sessions": 0, "messages": 0, "fts_rows": 0},
            "tokenizer": "none",
        },
        "warnings": [] if exists else ["记忆目录不存在"],
    }
    if layout.catalog.is_file():
        conn = catalog.connect(layout.catalog)
        try:
            result["catalog"]["stats"] = catalog.stats(conn)
            result["catalog"]["tokenizer"] = catalog.tokenizer_of(conn)
        finally:
            conn.close()
    return result
```

```python
# sessionpack/cli.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sessionpack import __version__, archive, catalog, diagnostics, export, importer
from sessionpack.adapters import get_adapter
from sessionpack.audit import ReadBoundaryError, ReadAudit
from sessionpack.paths import Layout, default_memory_root, ensure_layout, resolve_data_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3


def _layout(args) -> Layout:
    return ensure_layout(resolve_data_dir(args.data_dir))


def _memory_root(args) -> Path:
    return Path(args.memory_root) if args.memory_root else default_memory_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sessionpack",
                                     description="Trae 会话归档与同步：本地只读管理")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查数据源、索引与私有库状态")
    doctor.add_argument("--memory-root", default=None)
    doctor.add_argument("--data-dir", default=None)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    scan = sub.add_parser("scan", help="扫描记忆目录，不写索引")
    scan.add_argument("--memory-root", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=_cmd_scan)

    imp = sub.add_parser("import", help="导入记忆目录或归档包")
    imp.add_argument("--memory-root", default=None)
    imp.add_argument("--data-dir", default=None)
    imp.add_argument("--archive", default=None)
    imp.add_argument("--project", default=None)
    imp.add_argument("--since", default=None)
    imp.add_argument("--until", default=None)
    imp.add_argument("--reparse", action="store_true")
    imp.add_argument("--audit", action="store_true")
    imp.add_argument("--json", action="store_true")
    imp.set_defaults(func=_cmd_import)

    serve = sub.add_parser("serve", help="启动本地 Web UI")
    serve.add_argument("--memory-root", default=None)
    serve.add_argument("--data-dir", default=None)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    exp = sub.add_parser("export", help="导出会话为 Markdown 或 JSON")
    exp.add_argument("--memory-root", default=None)
    exp.add_argument("--data-dir", default=None)
    target = exp.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", default=None)
    target.add_argument("--project", default=None)
    target.add_argument("--all", action="store_true")
    exp.add_argument("--out", required=True)
    exp.add_argument("--format", choices=("markdown", "json"), default="markdown")
    exp.add_argument("--archive", action="store_true", help="生成 zip 归档包")
    exp.set_defaults(func=_cmd_export)

    ver = sub.add_parser("verify", help="校验归档包")
    ver.add_argument("archives", nargs="+")
    ver.add_argument("--json", action="store_true")
    ver.set_defaults(func=_cmd_verify)
    return parser
```

```python
# sessionpack/cli.py（续）
def _cmd_doctor(args) -> int:
    layout = _layout(args)
    data = diagnostics.collect(_memory_root(args), layout)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"SessionPack {__version__}")
        print(f"记忆目录：{data['memory_root']}"
              f"（{'存在' if data['memory_root_exists'] else '不存在'}）")
        source = data["source"]
        if source:
            print(f"适配器：{source['adapter_id']} v{source['adapter_version']}")
            print(f"项目 {source['projects']} 个（目录 {source['project_dirs']} 个）")
            print(f"topics.md {source['topic_files']} 个，"
                  f"jsonl {source['jsonl_files']} 个，{source['bytes_total']} 字节")
            if source["date_min"]:
                print(f"日期范围：{source['date_min']} 至 {source['date_max']}")
        private = data["private_db"]
        print(f"私有库：{private['path']}"
              f"（{'存在' if private['exists'] else '不存在'}，"
              f"{private['size']} 字节，不读取）")
        print(f"索引：{data['catalog']['path']}"
              f"（{'存在' if data['catalog']['exists'] else '未创建'}）")
        stats = data["catalog"]["stats"]
        print(f"索引统计：项目 {stats['projects']}，会话 {stats['sessions']}，"
              f"消息 {stats['messages']}，FTS {stats['fts_rows']}")
    return EXIT_OK if data["memory_root_exists"] else EXIT_USAGE


def _cmd_scan(args) -> int:
    root = _memory_root(args)
    if not root.is_dir():
        print(f"记忆目录不存在: {root}", file=sys.stderr)
        return EXIT_USAGE
    info = get_adapter().inspect(root)
    if args.json:
        print(json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"适配器：{info.adapter_id} v{info.adapter_version}")
        print(f"项目 {info.project_count} 个（目录 {info.project_dir_count} 个）")
        print(f"topics.md {info.topic_files} 个，jsonl {info.jsonl_files} 个，"
              f"{info.bytes_total} 字节")
        print(f"日期范围：{info.date_min} 至 {info.date_max}")
    return EXIT_OK


def _cmd_import(args) -> int:
    layout = _layout(args)
    if args.archive:
        conn = catalog.connect(layout.catalog)
        catalog.init_schema(conn)
        try:
            result = archive.import_archive(conn, Path(args.archive))
        except archive.ArchiveError as exc:
            print("归档校验失败: " + ", ".join(issue.code for issue in exc.issues),
                  file=sys.stderr)
            return EXIT_VERIFY
        finally:
            conn.close()
        print(f"归档 {result.archive_id}：会话 {result.sessions}，消息 {result.messages}，"
              f"新增消息 {result.messages_inserted}")
        return EXIT_OK

    root = _memory_root(args)
    if not root.is_dir():
        print(f"记忆目录不存在: {root}", file=sys.stderr)
        return EXIT_USAGE
    adapter = get_adapter()
    declared = adapter.declared_paths(root)
    kwargs = dict(reparse=args.reparse, project_filter=args.project,
                  since=args.since, until=args.until)
    if args.audit:
        with ReadAudit(root, declared) as audit:
            stats = importer.import_source(adapter, root, layout.catalog, **kwargs)
        try:
            audit.check(declared)
        except ReadBoundaryError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        if not args.json:
            print(f"读取边界：审计通过（打开 {len(audit.opened)} 个文件）")
    else:
        stats = importer.import_source(adapter, root, layout.catalog, **kwargs)

    if args.json:
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"导入完成：文件 {stats.files_processed}/{stats.files_declared}，"
              f"跳过 {stats.files_skipped}，消息 {stats.messages_inserted}，"
              f"主题 {stats.topics_applied}，失败 {stats.rows_failed} 行，"
              f"用时 {stats.elapsed_s:.2f}s")
        if stats.stopped:
            print("已按停止条件中断，下次运行将自动续接。")
    return EXIT_OK


def _cmd_serve(args) -> int:
    layout = _layout(args)
    if not layout.catalog.is_file():
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    from sessionpack.server import serve

    serve(layout.catalog, port=args.port, open_browser=not args.no_browser,
          memory_root=_memory_root(args))
    return EXIT_OK


def _cmd_export(args) -> int:
    layout = _layout(args)
    if not layout.catalog.is_file():
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    conn = catalog.connect(layout.catalog)
    out = Path(args.out)
    try:
        if args.archive:
            if args.session:
                print("归档不支持按会话生成，请用 --project 或 --all", file=sys.stderr)
                return EXIT_USAGE
            project_ids = ([args.project] if args.project
                           else [row["project_id"] for row in catalog.get_projects(conn)])
            for project_id in project_ids:
                item = archive.build_archive(conn, out, project_id)
                print(f"已生成 {item.path}（archive_id {item.archive_id}，"
                      f"会话 {item.sessions}，消息 {item.messages}）")
            return EXIT_OK
        if args.session:
            path = export.export_session(conn, args.session, out, args.format)
            print(f"已导出 {path}")
            return EXIT_OK
        if args.project:
            result = export.export_project(conn, args.project, out, args.format)
        else:
            result = export.export_all(conn, out, args.format)
        print(f"已导出 {result.sessions} 个会话、{result.messages} 条消息到 {out}")
        return EXIT_OK
    except KeyError:
        print(f"会话不存在: {args.session}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()


def _cmd_verify(args) -> int:
    results = [archive.verify_archive(Path(raw)) for raw in args.archives]
    if args.json:
        print(json.dumps([
            {"path": str(result.path), "ok": result.ok,
             "issues": [{"code": issue.code, "detail": issue.detail}
                        for issue in result.issues]}
            for result in results
        ], ensure_ascii=False, indent=2))
    else:
        for result in results:
            if result.ok:
                print(f"OK  {result.path}"
                      f"（archive_id {result.manifest['archive_id']}）")
            else:
                print(f"BAD {result.path}："
                      + ", ".join(issue.code for issue in result.issues))
    return EXIT_OK if all(result.ok for result in results) else EXIT_VERIFY


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_cli.py tests/test_audit.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/audit.py sessionpack/diagnostics.py sessionpack/cli.py tests/test_cli.py tests/test_audit.py
git commit -m "feat: 实现六命令 CLI、读取边界审计与诊断"
```

---

### Task 8: 本地 Web UI 三页与只读服务

**Files:**
- Create: `sessionpack/server.py`
- Create: `sessionpack/web/index.html`
- Create: `sessionpack/web/session.html`
- Create: `sessionpack/web/diagnostics.html`
- Create: `sessionpack/web/app.js`
- Create: `sessionpack/web/style.css`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `catalog.connect/list_sessions/get_session/get_messages/get_projects`、`summary.build_summary`、`diagnostics.collect`、`paths.ensure_layout`。
- Produces:
  - `server.create_server(catalog_path: Path, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None, memory_root: Path | None = None) -> ThreadingHTTPServer`，返回对象带 `token` 属性
  - `server.serve(catalog_path: Path, *, port: int = 8765, token: str | None = None, open_browser: bool = False) -> None`
  - HTTP 路由：`/`、`/session`、`/diagnostics`、`/app.js`、`/style.css`、`/api/sessions`、`/api/sessions/<id>`、`/api/diagnostics`

只绑定回环地址。`/api/*` 必须携带 token（`X-Token` 头或 `?token=`），页面与静态资源不校验。所有 HTML 内容通过 `textContent` 写入，不拼接 `innerHTML`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server.py
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source
from sessionpack.server import create_server


@pytest.fixture
def base_url(memory_tree: Path, tmp_path: Path):
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    server = create_server(db, port=0, token="test-token", memory_root=memory_tree)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Token", token)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def test_server_binds_loopback(base_url: str) -> None:
    assert base_url.startswith("http://127.0.0.1:")


def test_api_requires_token(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/api/sessions")
    assert exc.value.code == 403


def test_api_lists_sessions_with_header_token(base_url: str) -> None:
    status, body = _get(base_url + "/api/sessions", token="test-token")
    data = json.loads(body)
    assert status == 200
    assert len(data["sessions"]) == 2
    assert len(data["projects"]) == 2


def test_api_accepts_query_token(base_url: str) -> None:
    status, body = _get(base_url + "/api/sessions?token=test-token&q=归档")
    data = json.loads(body)
    assert status == 200
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["session_id"] == "a" * 24


def test_session_api_returns_summary_and_messages(base_url: str) -> None:
    status, body = _get(base_url + "/api/sessions/" + "a" * 24 + "?token=test-token")
    data = json.loads(body)
    assert status == 200
    assert data["summary"]["session_id"] == "a" * 24
    assert len(data["messages"]) == 2
    assert "实现归档包的校验流程" in data["summary"]["title"]


def test_session_api_rejects_bad_id(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/api/sessions/not-an-id?token=test-token")
    assert exc.value.code == 404


def test_diagnostics_api(base_url: str) -> None:
    status, body = _get(base_url + "/api/diagnostics?token=test-token")
    data = json.loads(body)
    assert status == 200
    assert data["adapter"]["id"] == "trae-cn-memory"
    assert data["catalog"]["stats"]["messages"] == 4


def test_pages_and_static_files_are_served(base_url: str) -> None:
    for path in ("/", "/session", "/diagnostics", "/app.js", "/style.css"):
        status, body = _get(base_url + path)
        assert status == 200
        assert body


def test_unknown_route_returns_404(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/nope")
    assert exc.value.code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_server.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'sessionpack.server'`

- [ ] **Step 3: 写实现**

```python
# sessionpack/server.py
from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sessionpack import catalog, diagnostics
from sessionpack.paths import default_memory_root, ensure_layout
from sessionpack.summary import build_summary

WEB_ROOT = Path(__file__).resolve().parent / "web"
SESSION_ID_RE = re.compile(r"^[0-9a-f]{24}$")
PAGES = {"/": "index.html", "/session": "session.html", "/diagnostics": "diagnostics.html"}
STATIC = {
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def create_server(
    catalog_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    memory_root: Path | None = None,
) -> ThreadingHTTPServer:
    catalog_path = Path(catalog_path)
    memory_root = Path(memory_root) if memory_root else default_memory_root()
    actual_token = token or secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SessionPack/0.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _authorized(self, query: dict) -> bool:
            supplied = self.headers.get("X-Token") or (query.get("token") or [""])[0]
            return secrets.compare_digest(supplied, actual_token)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            if path in PAGES:
                return self._page(PAGES[path])
            if path in STATIC:
                return self._static(*STATIC[path])
            if path.startswith("/api/"):
                if not self._authorized(query):
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "token 无效"})
                return self._api(path, query)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _page(self, name: str) -> None:
            self._send(HTTPStatus.OK, (WEB_ROOT / name).read_bytes(),
                       "text/html; charset=utf-8")

        def _static(self, name: str, content_type: str) -> None:
            self._send(HTTPStatus.OK, (WEB_ROOT / name).read_bytes(), content_type)

        def _api(self, path: str, query: dict) -> None:
            conn = catalog.connect(catalog_path)
            try:
                if path == "/api/sessions":
                    project = (query.get("project") or [None])[0]
                    term = (query.get("q") or [None])[0]
                    self._json(HTTPStatus.OK, {
                        "sessions": catalog.list_sessions(
                            conn, project_id=project, q=term, limit=200),
                        "projects": catalog.get_projects(conn),
                    })
                    return
                if path.startswith("/api/sessions/"):
                    session_id = path.rsplit("/", 1)[-1]
                    if not SESSION_ID_RE.match(session_id):
                        self._json(HTTPStatus.NOT_FOUND, {"error": "会话 ID 非法"})
                        return
                    session = catalog.get_session(conn, session_id)
                    if session is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "会话不存在"})
                        return
                    messages = catalog.get_messages(conn, session_id)
                    self._json(HTTPStatus.OK, {
                        "session": session,
                        "messages": messages,
                        "summary": build_summary(session, messages),
                    })
                    return
                if path == "/api/diagnostics":
                    layout = ensure_layout(catalog_path.parent)
                    self._json(HTTPStatus.OK, diagnostics.collect(memory_root, layout))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            finally:
                conn.close()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.token = actual_token
    return httpd


def serve(
    catalog_path: Path,
    *,
    port: int = 8765,
    token: str | None = None,
    open_browser: bool = False,
    memory_root: Path | None = None,
) -> None:
    httpd = create_server(catalog_path, port=port, token=token,
                          memory_root=memory_root)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={httpd.token}"
    print(f"SessionPack 已启动：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=[url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
```

```html
<!-- sessionpack/web/index.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SessionPack · 会话</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <h1>SessionPack</h1>
    <nav><a href="/diagnostics" id="diag-link">诊断</a></nav>
  </header>
  <main>
    <form id="search">
      <input id="q" type="search" placeholder="输入关键词（≥3 字符走全文索引）">
      <select id="project"><option value="">全部项目</option></select>
      <button type="submit">检索</button>
    </form>
    <p id="status"></p>
    <ul id="list"></ul>
  </main>
  <script src="/app.js"></script>
  <script>SessionPack.initIndex();</script>
</body>
</html>
```

```html
<!-- sessionpack/web/session.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SessionPack · 会话详情</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <h1><a href="/">SessionPack</a></h1>
    <nav><a href="/diagnostics" id="diag-link">诊断</a></nav>
  </header>
  <main>
    <article id="summary"></article>
    <section>
      <h2>消息</h2>
      <ol id="messages"></ol>
    </section>
  </main>
  <script src="/app.js"></script>
  <script>SessionPack.initSession();</script>
</body>
</html>
```

```html
<!-- sessionpack/web/diagnostics.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SessionPack · 诊断</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <h1><a href="/">SessionPack</a></h1>
  </header>
  <main>
    <h2>诊断</h2>
    <pre id="report"></pre>
  </main>
  <script src="/app.js"></script>
  <script>SessionPack.initDiagnostics();</script>
</body>
</html>
```

```javascript
// sessionpack/web/app.js
(function () {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";

  function withToken(path) {
    const url = new URL(path, window.location.origin);
    if (token) url.searchParams.set("token", token);
    return url.toString();
  }

  async function api(path) {
    const response = await fetch(withToken(path));
    if (!response.ok) throw new Error("请求失败：" + response.status);
    return response.json();
  }

  function link(path) {
    return token ? path + "?token=" + encodeURIComponent(token) : path;
  }

  async function initIndex() {
    const diag = document.getElementById("diag-link");
    if (diag) diag.href = link("/diagnostics");
    const status = document.getElementById("status");
    const list = document.getElementById("list");
    const select = document.getElementById("project");
    try {
      const data = await api("/api/sessions");
      data.projects.forEach(function (project) {
        const option = document.createElement("option");
        option.value = project.project_id;
        option.textContent = project.display_name + "（" + project.session_count + "）";
        select.appendChild(option);
      });
      renderSessions(data.sessions, list, status);
      document.getElementById("search").addEventListener("submit", async function (event) {
        event.preventDefault();
        const q = document.getElementById("q").value.trim();
        const project = select.value;
        const query = new URLSearchParams();
        if (q) query.set("q", q);
        if (project) query.set("project", project);
        const result = await api("/api/sessions?" + query.toString());
        renderSessions(result.sessions, list, status);
      });
    } catch (error) {
      status.textContent = error.message;
    }
  }

  function renderSessions(sessions, list, status) {
    list.replaceChildren();
    status.textContent = "共 " + sessions.length + " 个会话";
    sessions.forEach(function (session) {
      const item = document.createElement("li");
      const anchor = document.createElement("a");
      anchor.href = link("/session?id=" + encodeURIComponent(session.session_id));
      anchor.textContent = session.title || session.session_id;
      const meta = document.createElement("span");
      meta.textContent = " · " + (session.updated_at || session.created_at || "");
      item.appendChild(anchor);
      item.appendChild(meta);
      list.appendChild(item);
    });
  }

  async function initSession() {
    const diag = document.getElementById("diag-link");
    if (diag) diag.href = link("/diagnostics");
    const id = params.get("id") || "";
    const summaryBox = document.getElementById("summary");
    const list = document.getElementById("messages");
    try {
      const data = await api("/api/sessions/" + encodeURIComponent(id));
      summaryBox.replaceChildren();
      const title = document.createElement("h2");
      title.textContent = data.summary.title;
      const meta = document.createElement("p");
      meta.textContent = data.summary.project_name + " · " + data.summary.session_id +
        " · " + data.summary.message_count + " 条消息";
      summaryBox.appendChild(title);
      summaryBox.appendChild(meta);
      [["最新进展", data.summary.latest_outcome],
       ["关键结论", data.summary.learned.join("；")],
       ["涉及文件", data.summary.files.join("、")]].forEach(function (pair) {
        const heading = document.createElement("h3");
        heading.textContent = pair[0];
        const body = document.createElement("p");
        body.textContent = pair[1] || "（无记录）";
        summaryBox.appendChild(heading);
        summaryBox.appendChild(body);
      });
      data.messages.forEach(function (message) {
        const item = document.createElement("li");
        const head = document.createElement("strong");
        head.textContent = (message.ts || "") + " · " + (message.intent || "（无意图）");
        const outcome = document.createElement("p");
        outcome.textContent = message.outcome || "";
        item.appendChild(head);
        item.appendChild(outcome);
        list.appendChild(item);
      });
    } catch (error) {
      summaryBox.textContent = error.message;
    }
  }

  async function initDiagnostics() {
    const report = document.getElementById("report");
    try {
      const data = await api("/api/diagnostics");
      report.textContent = JSON.stringify(data, null, 2);
    } catch (error) {
      report.textContent = error.message;
    }
  }

  window.SessionPack = { initIndex: initIndex, initSession: initSession, initDiagnostics: initDiagnostics };
})();
```

```css
/* sessionpack/web/style.css */
:root {
  color-scheme: light dark;
  --fg: #1d1d1f;
  --bg: #fbfbfd;
  --muted: #6b6b70;
  --line: #d8d8dd;
  --accent: #0b6bcb;
}
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececf1; --bg: #17171a; --muted: #a0a0a8; --line: #33333a; }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 24px;
  max-width: 880px;
  font: 15px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif;
  color: var(--fg);
  background: var(--bg);
}
header { display: flex; align-items: center; justify-content: space-between; }
h1 { font-size: 20px; margin: 0; }
h2 { font-size: 17px; margin: 24px 0 8px; }
h3 { font-size: 14px; margin: 16px 0 4px; color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
nav a { margin-left: 12px; }
form { display: flex; gap: 8px; margin: 20px 0 8px; }
input, select, button {
  font: inherit;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: inherit;
}
input[type="search"] { flex: 1; }
button { cursor: pointer; color: var(--accent); }
#status, #list span { color: var(--muted); font-size: 13px; }
ul, ol { padding-left: 20px; }
li { margin: 6px 0; }
pre {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: auto;
  font-size: 12px;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_server.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add sessionpack/server.py sessionpack/web tests/test_server.py
git commit -m "feat: 实现本地只读 Web UI 与会话详情页"
```

---

### Task 9: 端到端验收（只读、续跑、归档往返）

**Files:**
- Create: `tests/helpers/crash_import.py`
- Test: `tests/test_acceptance.py`

**Interfaces:**
- Consumes: `importer.import_source`、`catalog.connect/init_schema/stats/get_projects`、`archive.build_archive/import_archive/verify_archive`、`export.export_project`。
- Produces: 无新接口，只固化验收断言。

这组测试对应 D1–D6 的验收条件：源目录不被修改、强杀后可续跑、归档往返后内容可检索且导出逐字节一致。

- [ ] **Step 1: 写失败测试与辅助脚本**

```python
# tests/helpers/crash_import.py
"""在指定行数处强制退出，用于验证断点续传。"""

import sys
from pathlib import Path

from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def main() -> None:
    root, db, crash_after, batch = sys.argv[1:5]
    import_source(
        TraeMemoryAdapter(),
        Path(root),
        Path(db),
        batch=int(batch),
        crash_after_rows=int(crash_after),
    )


if __name__ == "__main__":
    main()
```

```python
# tests/test_acceptance.py
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sessionpack import archive, catalog, export
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source

REPO_ROOT = Path(__file__).resolve().parents[1]
CRASH_HELPER = REPO_ROOT / "tests" / "helpers" / "crash_import.py"


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_import_never_modifies_source_tree(memory_tree: Path, tmp_path: Path) -> None:
    before = _snapshot(memory_tree)
    import_source(TraeMemoryAdapter(), memory_tree, tmp_path / "catalog.sqlite")
    assert _snapshot(memory_tree) == before


def test_hard_kill_then_resume_completes(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(memory_tree), str(db), "4", "1"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 9, proc.stderr
    conn = catalog.connect(db)
    partial = catalog.stats(conn)["messages"]
    conn.close()
    assert 0 < partial < 4

    import_source(TraeMemoryAdapter(), memory_tree, db, batch=1)
    conn = catalog.connect(db)
    try:
        assert catalog.stats(conn)["messages"] == 4
        done = conn.execute("SELECT COUNT(*) FROM import_files WHERE done=1").fetchone()[0]
        assert done == 4
    finally:
        conn.close()


def test_archive_roundtrip_and_export_is_stable(memory_tree: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    conn = catalog.connect(db)
    try:
        project = catalog.get_projects(conn)[0]
        built = archive.build_archive(conn, tmp_path / "archives", project["project_id"])
        before = export.export_project(conn, project["project_id"], tmp_path / "before")
    finally:
        conn.close()

    fresh = catalog.connect(tmp_path / "fresh.sqlite")
    catalog.init_schema(fresh)
    try:
        imported = archive.import_archive(fresh, built.path)
        after = export.export_project(fresh, project["project_id"], tmp_path / "after")
    finally:
        fresh.close()

    assert imported.sessions == built.sessions
    assert imported.messages == built.messages
    assert [p.name for p in before.paths] == [p.name for p in after.paths]
    for left, right in zip(before.paths, after.paths):
        assert left.read_text(encoding="utf-8") == right.read_text(encoding="utf-8")
    assert archive.verify_archive(built.path).ok
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_acceptance.py -v`
Expected: FAIL，`tests/helpers/crash_import.py` 不存在导致 `test_hard_kill_then_resume_completes` 报错；其余用例此时应已可运行

- [ ] **Step 3: 运行全部测试**

Run: `python -m pytest -q`
Expected: 全部通过（0 failed）

- [ ] **Step 4: 用真实数据跑一次完整链路**

先确认本机记忆目录存在，再依次执行并把输出贴进提交说明。

```bash
python -m sessionpack doctor
python -m sessionpack import --audit
python -m sessionpack export --all --out .tmp/export
python -m sessionpack export --all --archive --out .tmp/archives
python -m sessionpack verify .tmp/archives/*.zip
python -m sessionpack serve --no-browser --port 8765
```

Expected：`doctor` 显示 `trae-cn-memory` 与私有库“不读取”；`import --audit` 打印“读取边界：审计通过”；`export --archive` 为每个项目生成一个 zip；`verify` 输出 `OK`；`serve` 打印带 token 的 `127.0.0.1` 地址，浏览器打开后可检索与查看会话详情。最后用 Ctrl+C 退出。

- [ ] **Step 5: 提交**

```bash
git add tests/helpers/crash_import.py tests/test_acceptance.py
git commit -m "test: 补齐只读、强杀续跑与归档往返验收"
```

---

### Task 10: 文档同步

**Files:**
- Modify: `docs/未完成任务清单-20261105.md`（D1–D6 状态与验证命令）
- Modify: `项目构想-Trae会话归档与同步.md`（第 17 节验证清单、第 9 节风险、第 19 节变更记录）
- Create: `docs/使用说明-本地只读.md`

**Interfaces:**
- Consumes: Task 1–9 的实际命令、路径与退出码。
- Produces: 用户可照着操作的使用说明；任务清单中 D1–D6 标记为已完成并附验证命令。

- [ ] **Step 1: 更新未完成任务清单**

在 D1–D6 每项标题后追加状态行，格式固定为：

```markdown
- 状态：已完成（2026-11-05）
- 验证：`python -m pytest tests/test_<对应模块>.py -q`
```

D1 对应 `tests/test_acceptance.py`，D2 对应 `tests/test_catalog.py` 与 `tests/test_importer.py`，D3 对应 `tests/test_archive.py`，D4 对应 `tests/test_summary.py`，D5 对应 `tests/test_server.py`，D6 对应 `tests/test_adapter_trae_memory.py`。

- [ ] **Step 2: 更新构想稿验证清单、风险与变更记录**

在第 17 节“设计阶段的验证清单”末尾追加一段“实现验证（2026-11-05）”，逐条写明：只读哈希不变、`--audit` 无越界、强杀后续跑行数一致、归档篡改被拒绝、检索 P95 实测值。数据源限定为 `.trae-cn/memory` 白名单文件，私有库与 SOLO 不在读取范围。

在第 9 节“风险与待验证事项”末尾追加两条已发生风险及处理：

```markdown
- `topics.md` 会话覆盖不完整：只有 topics 的会话无正文，只有 jsonl 的会话标题回退到 `intent`。UI 在空字段处显示“（无记录）”，不做补全猜测。
- 私有库体积持续增长且格式加密：`doctor` 只报告路径与体积，不读取内容；接入前需另立 ADR。
```

在第 19 节“变更记录”末尾追加一行：

```markdown
- 2026-11-05：MVP 1 实现完成，六个子命令可用；归档格式 `sessionpack-archive` v1。
```

- [ ] **Step 3: 写使用说明**

```markdown
# SessionPack 使用说明（本地只读）

## 数据路径

- 记忆目录（只读）：`%USERPROFILE%\.trae-cn\memory`
- 索引与归档目录：`%LOCALAPPDATA%\SessionPack`
  - `catalog.sqlite`：可重建的索引
  - `archives/`：归档包输出目录（`export`、`import` 生成的 zip）
  - `temp/`：打包临时目录，失败后可整体删除
- 可用 `--data-dir` 覆盖索引目录，`--memory-root` 覆盖记忆目录。

## 隐私边界

- 只打开白名单文件：`topics.md`、`session_memory_*.jsonl`、各项目 `project_memory.md`、根 `user_profile.md`。
- 同目录的 `trae-jwt-token` 与账号标识目录不会被打开。`import --audit` 会记录本次实际打开的文件，越界立即失败。
- 私有库 `%APPDATA%\Trae CN\ModularData\ai-agent\database.db` 为加密格式，工具只报告其路径与体积，不解析、不复制。
- `doctor`、日志与导出内容不含 token；导出文件包含会话正文，分享前请自行确认范围。

## 兼容性

- Python 3.11 及以上；无运行时第三方依赖。
- FTS5 需要 SQLite 3.34+ 且提供 trigram 分词器；不可用时自动退回 LIKE 检索，`doctor` 会显示实际分词器。
- 适配器以 Trae CN 3.4.14 验证；其他版本按实验性支持，字段缺失时按原文导入并计入失败行。
- 归档格式 `format_version = 1`，读取方必须忽略不认识的字段；破坏性变更才递增版本号。

## 对 IDE 的影响

- 工具只读取记忆目录中的文本文件，不打开、不锁定 Trae 的私有数据库和 WAL。
- 读取发生在导入瞬间，按行流式处理，不会持续占用文件句柄。
- 导入过程中 Trae 可以继续使用；新写入的记忆文件会在下次 `import` 时按大小与修改时间增量更新。
```

- [ ] **Step 4: 检查文档中的命令可执行**

Run: `python -m sessionpack --help`
Expected：列出 `doctor`、`scan`、`import`、`serve`、`export`、`verify` 六个子命令，无异常

- [ ] **Step 5: 提交**

```bash
git add docs/未完成任务清单-20261105.md 项目构想-Trae会话归档与同步.md docs/使用说明-本地只读.md
git commit -m "docs: 同步 MVP 1 实现进度与本地只读使用说明"
```
