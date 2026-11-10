"""Trae 官方导出适配器：解析本地 .md 导出与分享链接渲染文本。

数据源分两类（B1 验证结论，见 ADR-008 与构想稿 20.6）：

1. 本地 .md 导出：`# 标题` + `> **Workspace:** <路径>` + `**User**`/`**Assistant**`
   交替。角色明确，无工具调用结构、无二进制附件。
2. 分享链接渲染文本：`# 标题` + 日期行 + 用户消息（无标记）+ 时间戳 +
   `TRAE` 标记 + AI 内容（含工具摘要行）。角色靠时间戳与 `TRAE` 判定，
   属启发式解析，稳定性低于 .md 导出。

两者都只产出 MessageRecord（角色写进 meta_json 的 ``role`` 字段），并前置
一条 TopicRecord 承载 ``# 标题`` 作为会话标题。会话/消息 ID 用 sha256 前
24 位派生，满足 catalog 的 `[0-9a-f]{24}` 约束并保证重导入幂等。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from sessionpack.model import (
    InspectResult,
    MessageRecord,
    Record,
    SourceFile,
    TopicRecord,
)

TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
WORKSPACE_RE = re.compile(r"^>\s*\*\*Workspace:\*\*\s*(.+?)\s*$")
MD_ROLE_RE = re.compile(r"^\*\*(User|Assistant)\*\*\s*$")
SHARE_TS_RE = re.compile(r"^\d{1,2}\s+\d{1,2},\s*\d{1,2}:\d{2}\s*$")
SHARE_AI_RE = re.compile(r"^TRAE\s*$")
# 分享链接里 AI 轮的元信息噪音行，导入时丢弃
_SHARE_NOISE = ("Worked for", "History Chats Compacted", "Switched to new request")

_EXPORT_SUFFIXES = (".md", ".txt")
_SHARE_PROJECT_ID = "trae-share"
_SHARE_DISPLAY_NAME = "Trae 分享"


def _sha24(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _parse_md_export(text: str) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    """解析本地 .md 导出 → (title, workspace, [(role, body), ...])。"""
    title: str | None = None
    workspace: str | None = None
    messages: list[tuple[str, str]] = []
    role: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if role is not None and buf:
            messages.append((role, "\n".join(buf).strip()))
            buf.clear()

    for line in text.splitlines():
        if title is None:
            m = TITLE_RE.match(line)
            if m:
                title = m.group(1).strip()
                continue
        m = WORKSPACE_RE.match(line)
        if m:
            workspace = m.group(1).strip()
            continue
        m = MD_ROLE_RE.match(line)
        if m:
            flush()
            role = m.group(1).lower()
            continue
        if role is not None and line.strip() == "---":
            continue  # 轮次分隔的水平线，非消息正文
        if role is not None:
            buf.append(line)
    flush()
    return title, workspace, messages


def _parse_share_export(text: str) -> tuple[str | None, list[tuple[str, str]]]:
    """解析分享链接渲染文本 → (title, [(role, body), ...])。

    启发式：时间戳行（`MM DD, HH:MM`）结束上一段、`TRAE` 开启 AI 段；
    时间戳之前累计的文本是用户消息，`TRAE` 之后的文本是 AI 消息。
    """
    title: str | None = None
    messages: list[tuple[str, str]] = []
    role = "user"  # 第一个时间戳之前的文本视为用户消息
    buf: list[str] = []
    seen_ai = False

    def flush() -> None:
        nonlocal buf
        if buf:
            messages.append((role, "\n".join(buf).strip()))
            buf = []

    for line in text.splitlines():
        if title is None:
            m = TITLE_RE.match(line)
            if m:
                title = m.group(1).strip()
                continue
        if "Content is AI-generated" in line:
            continue
        if SHARE_AI_RE.match(line):
            flush()
            role = "assistant"
            seen_ai = True
            continue
        if SHARE_TS_RE.match(line):
            flush()
            role = "user"
            continue
        if seen_ai and role == "assistant" and any(
            line.startswith(noise) for noise in _SHARE_NOISE
        ):
            continue
        buf.append(line)
    flush()
    return title, messages


def _detect_format(text: str) -> str:
    """返回 'md' 或 'share'。以是否出现 `**User**`/`**Assistant**` 角色标记为准。"""
    for line in text.splitlines():
        if MD_ROLE_RE.match(line):
            return "md"
        if SHARE_AI_RE.match(line):
            return "share"
    return "md"  # 缺省按 md 处理


def _workspace_of(text: str, fmt: str) -> str:
    if fmt == "share":
        return _SHARE_PROJECT_ID
    for line in text.splitlines():
        m = WORKSPACE_RE.match(line)
        if m:
            return m.group(1).strip()
    return "trae-export"


class TraeExportAdapter:
    id = "trae-export"
    version = 1
    source_kind = "trae-export"

    def detect(self, root: Path) -> bool:
        return root.is_dir() and any(
            p.is_file() and p.suffix.lower() in _EXPORT_SUFFIXES
            for p in root.iterdir()
        )

    def declared_paths(self, root: Path) -> list[Path]:
        return sorted(
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in _EXPORT_SUFFIXES
        )

    def enumerate(self, root: Path) -> Iterator[SourceFile]:
        for path in self.declared_paths(root):
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue
            fmt = _detect_format(text)
            project_id = _workspace_of(text, fmt)
            yield SourceFile(path, project_id, project_id, "messages")

    def inspect(self, root: Path) -> InspectResult:
        declared = self.declared_paths(root)
        total = 0
        for path in declared:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return InspectResult(
            adapter_id=self.id,
            adapter_version=self.version,
            source_kind=self.source_kind,
            root=root,
            project_count=len({self._project_id_of(p) for p in declared}),
            project_dir_count=len(declared),
            topic_files=0,
            jsonl_files=len(declared),
            bytes_total=total,
            date_min=None,
            date_max=None,
        )

    def _project_id_of(self, path: Path) -> str:
        try:
            text = path.read_text("utf-8")
        except OSError:
            return "trae-export"
        return _workspace_of(text, _detect_format(text))

    def display_name(self, canonical_project_id: str) -> str:
        if canonical_project_id == _SHARE_PROJECT_ID:
            return _SHARE_DISPLAY_NAME
        return canonical_project_id

    def read_file(
        self, ref: SourceFile, start_offset: int = 0, start_seq: int = 0
    ) -> Iterator[tuple[int, Record | None, str | None]]:
        try:
            text = ref.path.read_text("utf-8")
        except OSError as exc:
            yield 0, None, (exc.strerror or str(exc))[:60]
            return
        fmt = _detect_format(text)
        if fmt == "md":
            title, workspace, messages = _parse_md_export(text)
        else:
            title, messages = _parse_share_export(text)

        project_id = ref.project_id
        session_id = _sha24(
            f"{project_id}\n{title or ''}\n" + "\n".join(f"{r}:{b}" for r, b in messages)
        )
        try:
            topic_time = datetime.fromtimestamp(ref.path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S")
        except OSError:
            topic_time = "1970-01-01 00:00:00"
        offset = 0
        seq = start_seq

        if title:
            yield offset, TopicRecord(
                session_id=session_id,
                topic_time=topic_time,
                body=title,
                project_id=project_id,
                source_kind=self.source_kind,
            ), None
            offset += 1

        for role, body in messages:
            if not body:
                continue
            seq += 1
            offset += 1
            yield offset, MessageRecord(
                session_id=session_id,
                message_id=_sha24(f"{session_id}:{seq}"),
                seq=seq,
                ts=None,
                intent=None,
                outcome=None,
                actions=(),
                learned=(),
                meta={"role": role},
                body=body,
                project_id=project_id,
                source_kind=self.source_kind,
            ), None
