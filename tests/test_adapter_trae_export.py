"""Trae 官方导出适配器（.md 与分享链接）的解析与导入契约测试。"""

from __future__ import annotations

from pathlib import Path

from sessionpack.adapters.trae_export import (
    TraeExportAdapter,
    _parse_md_export,
    _parse_share_export,
)
from sessionpack.model import MessageRecord, SourceFile, TopicRecord

MD_SAMPLE = """# 分析并确定下一步计划

> **Workspace:** e:\\Workplace\\SessionPack

---

**User**

Use Skill: brainstorming

**Assistant**

Let me explore first.

---

**User**

继续
"""

SHARE_SAMPLE = """# 分析并确定下一步计划

November 10, 2026·Content is AI-generated and may not be fully accurate

好的，开始 MVP 1.2 的待办任务

11 06, 01:04

TRAE

Worked for 4h 57m

History Chats Compacted

Searched files 2 times

仓库和记忆里都没有直接命中。

Read 1 file, Ran 2 commands

意外中断, 请继续

11 06, 10:23

TRAE

继续收尾。

Ran 2 commands
"""


def test_parse_md_export():
    title, workspace, messages = _parse_md_export(MD_SAMPLE)
    assert title == "分析并确定下一步计划"
    assert workspace == "e:\\Workplace\\SessionPack"
    assert messages == [
        ("user", "Use Skill: brainstorming"),
        ("assistant", "Let me explore first."),
        ("user", "继续"),
    ]


def test_parse_share_export():
    title, messages = _parse_share_export(SHARE_SAMPLE)
    assert title == "分析并确定下一步计划"
    # 第一个时间戳前的文本是用户消息（可靠）
    assert ("user", "好的，开始 MVP 1.2 的待办任务") in messages
    # 全文被捕获（可检索）；噪音行（Worked for / History Chats Compacted）被丢弃
    joined = "\n".join(b for _, b in messages)
    assert "仓库和记忆里都没有直接命中。" in joined
    assert "继续收尾。" in joined
    assert "Worked for" not in joined
    assert "History Chats Compacted" not in joined


def _records(adapter: TraeExportAdapter, text: str):
    path = Path("tmp-export.md")
    ref = SourceFile(path, "p", "p", "messages")
    # 通过临时文件让 read_file 走真实读文件路径
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        ref = SourceFile(tmp, "p", "p", "messages")
        return list(adapter.read_file(ref))
    finally:
        tmp.unlink(missing_ok=True)


def test_read_file_emits_title_and_roles():
    adapter = TraeExportAdapter()
    out = _records(adapter, MD_SAMPLE)
    records = [r for _, r, err in out if err is None and r is not None]
    # 首条是 TopicRecord 承载标题
    assert isinstance(records[0], TopicRecord)
    assert records[0].body == "分析并确定下一步计划"
    # 其余是 MessageRecord，角色在 meta["role"]
    messages = [r for r in records if isinstance(r, MessageRecord)]
    assert [m.meta["role"] for m in messages] == ["user", "assistant", "user"]
    # session_id / message_id 满足 24 位 hex
    sid = records[0].session_id
    assert len(sid) == 24 and all(c in "0123456789abcdef" for c in sid)
    for m in messages:
        assert len(m.message_id) == 24
        assert m.session_id == sid


def test_read_file_ids_stable():
    adapter = TraeExportAdapter()
    out1 = _records(adapter, MD_SAMPLE)
    out2 = _records(adapter, MD_SAMPLE)
    r1 = [r for _, r, _ in out1 if r is not None]
    r2 = [r for _, r, _ in out2 if r is not None]
    assert [r.session_id for r in r1] == [r.session_id for r in r2]
