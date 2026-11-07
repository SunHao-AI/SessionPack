"""lint_docs 自测：真断链报错 / 行内代码与代码块不误报 / file:// 与绝对路径报错 / 正常链接放行 / 排除规则。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lint_docs  # noqa: E402


def test_broken_relative_link_reported(tmp_path):
    (tmp_path / "doc.md").write_text("见 [x](missing.md)。\n", encoding="utf-8")
    out = lint_docs.check_source("见 [x](missing.md)。\n", "doc.md", tmp_path)
    assert len(out) == 1
    assert out[0].startswith("doc.md:1: E_DOC_LINK")
    assert "missing.md" in out[0]


def test_inline_code_not_misread_as_link():
    # 计划文档 :1669 的实测误报形态：行内代码里的 `](`
    src = "调用 `globals()[f\"_cmd_{args.command}\"](args)` 分发。\n"
    assert lint_docs.check_source(src, "plans/x.md", Path(".")) == []


def test_code_fence_not_scanned():
    src = "示例：\n```markdown\n[链接](missing.md)\n```\n结束。\n"
    assert lint_docs.check_source(src, "doc.md", Path(".")) == []


def test_file_url_and_absolute_paths_rejected():
    base = Path(".")
    out = lint_docs.check_source(
        "[a](file:///d:/WorkPlace/x.md)\n", "doc.md", base)
    assert len(out) == 1 and "file://" in out[0]
    out = lint_docs.check_source("[b](d:/WorkPlace/x.md)\n", "doc.md", base)
    assert len(out) == 1 and "绝对路径" in out[0]
    out = lint_docs.check_source("[c](/home/u/x.md)\n", "doc.md", base)
    assert len(out) == 1 and "绝对路径" in out[0]


def test_valid_links_pass(tmp_path):
    (tmp_path / "target.md").write_text("ok\n", encoding="utf-8")
    src = ("[ok](target.md)\n[锚](target.md#节)\n[节](#本地锚点)\n"
           "[网](https://example.com/x)\n[邮](mailto:a@b.c)\n"
           "[目录](sub/)\n")
    (tmp_path / "sub").mkdir()
    assert lint_docs.check_source(src, "doc.md", tmp_path) == []


def test_exclusion_rules(tmp_path):
    (bad := tmp_path / ".venv").mkdir()
    (bad / "x.md").write_text("[b](nope.md)\n", encoding="utf-8")
    (bad := tmp_path / "spike").mkdir()
    (bad / "x.md").write_text("[b](nope.md)\n", encoding="utf-8")
    (good_dir := tmp_path / "docs").mkdir()
    (good_dir / "ok.md").write_text("[b](nope2.md)\n", encoding="utf-8")
    rels = [p.relative_to(tmp_path).as_posix()
            for p in lint_docs.iter_markdown(tmp_path)]
    assert rels == ["docs/ok.md"]


def test_repo_tree_clean():
    """全仓实跑必须 0 违规（并入 check.py 的前提）。"""
    assert lint_docs.check_tree(lint_docs.ROOT) == []
