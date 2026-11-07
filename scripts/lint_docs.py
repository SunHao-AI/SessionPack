"""文档断链体检：相对链接存在性 + 禁 file:///绝对路径。仅 stdlib。

用法:  python scripts/lint_docs.py
输出格式: 文件:行: E_DOC_LINK 说明
不引入基线——断链必须当场修，不存在"合法的断链"。
规则见 docs/superpowers/specs/2026-11-07-架构工程化-配置边界与文档护栏-design.md §5。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 排除目录（首段命中即排除）；点开头目录另行统一排除。
EXCLUDED_TOP = frozenset({"spike", "build", "dist", "node_modules"})

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
# 行内代码 span：支持一个或多个反引号作定界符（内容不含同长反引号串）。
CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)*?\1")
# 机器绝对路径：Windows 盘符 / UNC / POSIX 根开头（POSIX 单独要求像路径，
# 避免把 "[注](1/2)" 之类非路径文本误判）。
ABS_WIN_RE = re.compile(r"^[A-Za-z]:[\\/]")
ABS_POSIX_RE = re.compile(r"^/[A-Za-z0-9_.\-]+/")


def _strip_inline_code(line: str) -> str:
    """剥掉行内代码 span，防止其中的 `](` 被当成链接语法。"""
    return CODE_SPAN_RE.sub("", line)


def _is_ignored(target: str) -> bool:
    """外链 / 纯锚点不参与存在性检查。"""
    low = target.lower()
    return low.startswith(("http://", "https://", "mailto:")) or (
        target.startswith("#") and "#" not in target[1:])


def check_source(src: str, rel: str, base: Path) -> list[str]:
    """对单个 md 文本返回违规列表；base 为相对链接的解析基准目录。"""
    out: list[str] = []
    in_fence = False
    for lineno, raw in enumerate(src.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in LINK_RE.finditer(_strip_inline_code(raw)):
            target = m.group(1).strip()
            if not target or _is_ignored(target):
                continue
            if target.startswith("file://"):
                out.append(f"{rel}:{lineno}: E_DOC_LINK file:// 链接不可移植，"
                           f"请改写为相对当前文件的路径: {target}")
                continue
            if ABS_WIN_RE.match(target) or target.startswith("\\\\") \
                    or ABS_POSIX_RE.match(target):
                out.append(f"{rel}:{lineno}: E_DOC_LINK 机器绝对路径链接不可移植，"
                           f"请改写为相对当前文件的路径: {target}")
                continue
            path_part = target.split("#", 1)[0].strip()
            if not path_part:
                continue  # 纯锚点
            if not (base / path_part).exists():
                out.append(f"{rel}:{lineno}: E_DOC_LINK 目标不存在: {path_part}")
    return out


def iter_markdown(root: Path) -> list[Path]:
    """受检 md 清单：排除任一路径段以 `.` 开头 + EXCLUDED_TOP。"""
    files: list[Path] = []
    for p in sorted(root.rglob("*.md")):
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if rel_parts[0] in EXCLUDED_TOP:
            continue
        files.append(p)
    return files


def check_tree(root: Path) -> list[str]:
    """全仓体检：对每个受检 md 调用 check_source。"""
    out: list[str] = []
    for p in iter_markdown(root):
        rel = p.relative_to(root).as_posix()
        out.extend(check_source(p.read_text(encoding="utf-8"), rel, p.parent))
    return out


def main(argv: list[str]) -> int:
    violations = check_tree(ROOT)
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
