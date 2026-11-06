"""B3 最小口径扫描：按项目统计记忆条目与摘要覆盖（只读，不写任何文件）。

用法：python scripts/memory_coverage_scan.py [memory_root]
口径：项目内存在 session_memory_*.jsonl → 有记忆条目；无 jsonl 但
topics.md 含 session_id 条目 → 只有摘要；两者皆无 → 都没有。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sessionpack.paths import default_memory_root  # noqa: E402

SESSION_RE = re.compile(r"session_memory_([0-9a-f]{24})\.jsonl")
TOPIC_LINE_RE = re.compile(r"^\[session_id:")


def scan(root: Path) -> dict:
    base = root / "projects" if (root / "projects").is_dir() else root
    projects: dict[str, dict] = {}
    for project in sorted(p for p in base.iterdir() if p.is_dir()):
        sessions = {
            m.group(1)
            for f in project.rglob("session_memory_*.jsonl")
            if (m := SESSION_RE.fullmatch(f.name))
        }
        topic_entries = 0
        for topic_file in project.rglob("topics.md"):
            for line in topic_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if TOPIC_LINE_RE.match(line.strip()):
                    topic_entries += 1
        if sessions:
            category = "有记忆条目"
        elif topic_entries:
            category = "只有摘要"
        else:
            category = "都没有"
        projects[project.name] = {
            "category": category,
            "memory_sessions": len(sessions),
            "topic_entries": topic_entries,
        }
    totals = {
        "projects": len(projects),
        "memory_sessions": sum(v["memory_sessions"] for v in projects.values()),
        "summary_only": sum(
            1 for v in projects.values() if v["category"] == "只有摘要"
        ),
        "none": sum(1 for v in projects.values() if v["category"] == "都没有"),
    }
    return {"projects": projects, "totals": totals}


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else default_memory_root()
    if not root.is_dir():
        print(f"记忆目录不存在: {root}")
        return 2
    report = scan(root)
    for name, info in report["projects"].items():
        print(
            f"{name}\t{info['category']}"
            f"\t记忆条目={info['memory_sessions']}"
            f"\t摘要条目={info['topic_entries']}"
        )
    print("TOTAL", report["totals"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
