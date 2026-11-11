"""Trae 官方导出适配器（.md 与分享链接）的解析与导入契约测试。"""

from __future__ import annotations

import json
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


SHARE_IMAGE_SAMPLE = """# 测试分享

November 10, 2026·Content is AI-generated and may not be fully accurate

请看这张架构图

11 06, 01:04

TRAE

图中说明了整体结构。
"""


def _share_records(tmp_path, manifest: dict | None):
    """写 <slug>.txt（+可选 images/<slug>/manifest.json）并解析。"""
    slug = "abc123"
    txt = tmp_path / f"{slug}.txt"
    txt.write_text(SHARE_IMAGE_SAMPLE, encoding="utf-8")
    if manifest is not None:
        mdir = tmp_path / "images" / slug
        mdir.mkdir(parents=True)
        (mdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    adapter = TraeExportAdapter()
    ref = SourceFile(txt, "trae-share", "trae-share", "messages")
    return [r for _, r, err in adapter.read_file(ref) if r is not None]


def test_share_images_attached_by_snippet(tmp_path):
    manifest = {"slug": "abc123", "images": [
        {"file": "abc123_0.png", "url": "https://cdn/a.png",
         "snippet": "请看这张架构图"},
        {"file": "abc123_1.png", "url": "https://cdn/b.png",
         "snippet": "完全匹配不到的片段XYZ"},
    ]}
    records = _share_records(tmp_path, manifest)
    messages = [r for r in records if isinstance(r, MessageRecord)]
    sid = messages[0].session_id
    user = next(m for m in messages if m.meta["role"] == "user")
    # snippet 归一化包含匹配挂到用户消息
    assert user.meta["images"] == [f"{sid}/abc123_0.png"]
    assert user.meta["slug"] == "abc123"
    # 匹配不到兜底挂最后一条
    assert messages[-1].meta["images"] == [f"{sid}/abc123_1.png"]


def test_share_without_manifest_no_images(tmp_path):
    records = _share_records(tmp_path, None)
    messages = [r for r in records if isinstance(r, MessageRecord)]
    assert all("images" not in m.meta for m in messages)


def test_share_manifest_does_not_change_session_id(tmp_path):
    bare = _share_records(tmp_path, None)
    with_manifest = _share_records(
        tmp_path, {"slug": "abc123", "images": [
            {"file": "abc123_0.png", "url": "u", "snippet": "请看这张架构图"}]})
    assert bare[0].session_id == with_manifest[0].session_id


def test_md_export_meta_unaffected_by_share_keys(tmp_path):
    adapter = TraeExportAdapter()
    out = _records(adapter, MD_SAMPLE)
    messages = [r for _, r, _ in out if isinstance(r, MessageRecord)]
    assert all(set(m.meta) == {"role"} for m in messages)
