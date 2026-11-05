#!/usr/bin/env python3
"""27 MB 合成集压测编排：基线、幂等、中断恢复、检索响应、只读与读取边界。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SPIKE_DATA_DIR", r"D:\cache\.cache\sessionpack-spike"))
MEMORY = DATA / "memory"
GEN_STATS = DATA / "gen_stats.json"
RESULTS = DATA / "results.json"

QUERIES = ["FastAPI", "SQLite", "manifest", "解析器", "归档包", "导入任务", "目录归并", "字段映射"]


def python() -> str:
    return sys.executable


def reset_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)


def run_import(db: Path, report: Path, extra: list[str] | None = None) -> tuple[int, dict | None]:
    cmd = [
        python(), str(HERE / "memory_import.py"),
        "--memory-root", str(MEMORY),
        "--db", str(db),
        "--report", str(report),
        *(extra or []),
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    wall = time.perf_counter() - started
    data = None
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        data["wall_s"] = round(wall, 2)
    if proc.returncode not in (0, 9):
        print(proc.stdout[-2000:], proc.stderr[-2000:], file=sys.stderr)
    return proc.returncode, data


def snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, h.hexdigest())
    return snap


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return round(ordered[idx], 1)


def bench_search(db: Path, queries: list[str], repeats: int = 5) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    has_fts = bool(
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='message_fts'").fetchone()[0]
    )
    like_ms: list[float] = []
    fts_ms: list[float] = []
    detail = []
    for q in queries:
        hits_like = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE body LIKE ? LIMIT 50)",
            (f"%{q}%",),
        ).fetchone()[0]
        for _ in range(repeats):
            t = time.perf_counter()
            rows = len(conn.execute(
                "SELECT session_id FROM messages WHERE body LIKE ? LIMIT 50", (f"%{q}%",)
            ).fetchall())
            like_ms.append((time.perf_counter() - t) * 1000)
        item = {"query": q, "like_hits": hits_like, "like_rows": rows}
        if has_fts and len(q) >= 3:
            hits_fts = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM message_fts WHERE message_fts MATCH ? LIMIT 50)",
                (f'"{q}"',),
            ).fetchone()[0]
            for _ in range(repeats):
                t = time.perf_counter()
                fts_rows = len(conn.execute(
                    "SELECT session_id FROM message_fts WHERE message_fts MATCH ? LIMIT 50",
                    (f'"{q}"',),
                ).fetchall())
                fts_ms.append((time.perf_counter() - t) * 1000)
            item.update({"fts_hits": hits_fts, "fts_rows": fts_rows})
        detail.append(item)
    conn.close()
    return {
        "queries": len(queries),
        "repeats": repeats,
        "fts": has_fts,
        "like_p50_ms": pct(like_ms, 0.5),
        "like_p95_ms": pct(like_ms, 0.95),
        "fts_p50_ms": pct(fts_ms, 0.5),
        "fts_p95_ms": pct(fts_ms, 0.95),
        "max_ms": round(max(like_ms + fts_ms or [0]), 1),
        "detail": detail,
    }


def partial_progress(db: Path) -> dict:
    """强杀后直接读库，统计已提交行数与断点位置。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT (SELECT COUNT(*) FROM messages) + (SELECT COUNT(*) FROM topic_revisions)"
    ).fetchone()[0]
    total = conn.execute("SELECT COALESCE(SUM(rows),0) FROM import_files").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM import_files WHERE done=1").fetchone()[0]
    files = conn.execute("SELECT COUNT(*) FROM import_files").fetchone()[0]
    conn.close()
    return {"rows": rows, "rows_total": total, "files_done": done, "files_seen": files}


def integrity(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "topics": conn.execute("SELECT COUNT(*) FROM topic_revisions").fetchone()[0],
        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        "dup_message_ids": conn.execute(
            "SELECT COUNT(*) FROM (SELECT session_id, message_id FROM messages"
            " GROUP BY 1,2 HAVING COUNT(*) > 1)"
        ).fetchone()[0],
        "messages_without_session": conn.execute(
            "SELECT COUNT(*) FROM messages m LEFT JOIN sessions s USING(session_id)"
            " WHERE s.session_id IS NULL"
        ).fetchone()[0],
        "sessions_without_project": conn.execute(
            "SELECT COUNT(*) FROM sessions s LEFT JOIN projects p USING(project_id)"
            " WHERE p.project_id IS NULL"
        ).fetchone()[0],
        "sessions_without_content": conn.execute(
            "SELECT COUNT(*) FROM sessions s WHERE s.title IS NULL"
        ).fetchone()[0],
    }
    conn.close()
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    results: dict = {"data_dir": str(DATA), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    if not MEMORY.is_dir() or "--regen" in sys.argv:
        print("生成合成数据 ...")
        subprocess.run(
            [python(), str(HERE / "gen_synthetic.py"), "--out", str(MEMORY), "--force"],
            check=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
    gen = json.loads(GEN_STATS.read_text(encoding="utf-8"))
    results["dataset"] = gen
    print(f"合成集：{gen['mb']} MB / {gen['files']} 文件 / "
          f"{gen['sessions']} 会话 / {gen['jsonl_records']} 条 jsonl / "
          f"{gen['topics_records']} 条 topics")

    before = snapshot(MEMORY)
    before_bytes = sum(v[0] for v in before.values())

    db_a = DATA / "db" / "a.sqlite"
    reset_db(db_a)
    print("基线导入 ...")
    rc_a, rep_a = run_import(db_a, DATA / "report_a.json")
    assert rc_a == 0 and rep_a, f"基线导入失败 rc={rc_a}"
    results["baseline"] = rep_a
    results["baseline"]["throughput_mb_s"] = round(gen["bytes"] / 1024 / 1024 / rep_a["elapsed_s"], 2)
    results["baseline"]["throughput_rows_s"] = int(rep_a["rows_read"] / rep_a["elapsed_s"])

    print("去重幂等：重扫同一数据源 ...")
    rc_b, rep_b = run_import(db_a, DATA / "report_b.json", ["--reparse"])
    assert rc_b == 0 and rep_b
    results["idempotent"] = rep_b

    print("中断恢复：首次运行中途强杀 ...")
    db_b = DATA / "db" / "b.sqlite"
    reset_db(db_b)
    rc_c1, rep_c1 = run_import(
        db_b, DATA / "report_c1.json", ["--crash-after-rows", "15000"]
    )
    # 进程被强杀时不会写报告，改为直接读库统计已提交进度
    crash_progress = partial_progress(db_b)
    results["crash"] = {"exit_code": rc_c1, "committed": crash_progress, **(rep_c1 or {})}
    print(f"  强杀后已提交：{crash_progress}")
    assert crash_progress["rows"] > 0, "中断后应保留已提交批次，当前为 0"
    # 与整批导入的总行数比较，确认强杀确实落在中途而非末尾
    assert crash_progress["rows"] < rep_a["rows_read"], "中断点未落在导入中途"
    print("中断恢复：断点续跑 ...")
    rc_c2, rep_c2 = run_import(db_b, DATA / "report_c2.json")
    assert rc_c2 == 0 and rep_c2
    results["resume"] = rep_c2

    base_counts = {k: rep_a[k] for k in ("sessions", "messages", "topics", "projects")}
    resume_counts = {k: rep_c2[k] for k in ("sessions", "messages", "topics", "projects")}
    results["recovery_equal"] = base_counts == resume_counts
    results["baseline_counts"] = base_counts
    results["resume_counts"] = resume_counts
    results["integrity"] = integrity(db_a)

    after = snapshot(MEMORY)
    after_bytes = sum(v[0] for v in after.values())
    changed = sum(1 for k, v in before.items() if after.get(k) != v)
    results["readonly"] = {
        "files": len(before),
        "changed_files": changed,
        "added_files": len(set(after) - set(before)),
        "removed_files": len(set(before) - set(after)),
        "bytes_before": before_bytes,
        "bytes_after": after_bytes,
    }

    print("检索响应 ...")
    results["search"] = bench_search(db_a, QUERIES)

    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    b = results["baseline"]
    print("\n## 压测结果\n")
    print("| 项目 | 实测 |")
    print("|---|---|")
    print(f"| 合成集 | {gen['mb']} MB / {gen['files']} 文件 / {gen['sessions']} 会话 |")
    print(f"| 记录数 | {gen['jsonl_records']} 条 jsonl + {gen['topics_records']} 条 topics |")
    print(f"| 首次导入 | {b['elapsed_s']} s，{b['throughput_mb_s']} MB/s，"
          f"{b['throughput_rows_s']} 行/s |")
    print(f"| 峰值内存 | {b['peak_rss_mb']} MB |")
    print(f"| 索引体积 | {b['db_size_mb']} MB |")
    print(f"| 失败行 | {b['rows_failed']}（样例：{b['failures']}） |")
    print(f"| 二次重扫 | 新增会话 {rep_b['sessions_new']}，新增行 {rep_b['rows_inserted']}，"
          f"跳过 {rep_b['rows_skipped']} |")
    print(f"| 中断 | 退出码 {rc_c1}，强杀时已提交 {crash_progress['rows']} 行，"
          f"完成 {crash_progress['files_done']}/{crash_progress['files_seen']} 文件 |")
    print(f"| 续跑 | 处理 {rep_c2['files_processed']} 文件，跳过已完成 "
          f"{rep_c2['skipped_unchanged_files']}，续传 {rep_c2['resumed_files']} |")
    print(f"| 计数一致 | {results['recovery_equal']}（基线 {base_counts}） |")
    print(f"| 源目录 | {changed} 个文件变更，{len(set(after) - set(before))} 新增 |")
    print(f"| 读取边界 | 声明 {b['declared_files']}，实际打开 {b['opened_under_source']}，"
          f"越界 {len(b['unexpected_opens'])} |")
    s = results["search"]
    print(f"| 检索 P95 | LIKE {s['like_p95_ms']} ms / FTS {s['fts_p95_ms']} ms |")
    print(f"\n结果文件：{RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
