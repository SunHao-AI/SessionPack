#!/usr/bin/env python3
"""生成 27 MB 量级合成记忆目录，用于 Trae 记忆目录适配器压测。

目录层级、文件命名、字段集合、字段缺失比例、目录后缀分片与覆盖缺口，
均按 docs/适配器字段结构报告-trae-cn-memory.md 的实测分布模拟。
全部文本由固定词表组合而成，不含任何真实会话内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DATE = date(2026, 7, 22)
SPAN_DAYS = 106  # 2026-07-22 .. 2026-11-04

PROJECTS = [
    ("d", ["WorkPlace", "Pycharm", "FastAPI-Hao"]),
    ("d", ["WorkPlace", "Pycharm", "flysee-video-setting"]),
    ("d", ["WorkPlace", "Pycharm", "SessionPack"]),
    ("d", ["WorkPlace", "Pycharm", "modelctl"]),
    ("d", ["WorkPlace", "Pycharm", "pyarmor"]),
    ("c", ["Users", "dev", "scripts", "data-pipeline"]),
    ("d", ["projects", "web-console"]),
    ("d", ["projects", "docs-site"]),
    ("e", ["workspace", "mobile-shell"]),
]
# 这些项目会额外生成 --p2-<16 位哈希> 目录，模拟同一仓库被拆成两份
SPLIT_PROJECTS = {1, 2, 4, 6}

NOUNS = [
    "索引", "解析器", "归档包", "配置加载", "搜索接口", "导入任务", "会话条目",
    "字段映射", "目录归并", "去重逻辑", "批处理", "断点续传", "日志输出",
    "元数据表", "校验和", "时间戳", "项目标识", "摘要文本", "查询计划",
    "缓存层", "错误码", "进度条", "导出模板", "命令行入口", "测试夹具",
]
VERBS = [
    "重写", "拆分", "补齐", "校验", "合并", "迁移", "简化", "加固",
    "梳理", "重构", "定位", "复现", "压测", "抽样", "回滚",
]
OBJECTS = [
    "流式读取路径", "SQLite 写入批次", "FTS 索引", "路径白名单", "版本判断",
    "会话聚合逻辑", "重复导入场景", "中断恢复流程", "manifest 结构",
    "字段可选性", "目录编码规则", "内存峰值采样", "吞吐统计", "失败记录",
]
ENDINGS = [
    "避免整库入内存", "保证二次导入不新增", "保持源文件只读",
    "在测试中覆盖空字段", "让失败项单独计数", "与实测字段分布对齐",
    "减少一次全表扫描", "把异常限制在单条记录",
]
TOKENS = [
    "FastAPI", "SQLite", "FTS5", "WAL", "PowerShell", "pytest", "zip",
    "manifest", "session_id", "jsonl", "topics.md", "hashlib",
]


def encode_project(drive: str, segments: list[str]) -> str:
    return "-" + "-".join([drive, *segments])


def suffixed(name: str, idx: int) -> str:
    return f"{name}--p2-{hashlib.sha1(f'p{idx}'.encode()).hexdigest()[:16]}"


def sentence(rng: random.Random, n: int = 1) -> str:
    parts = []
    for _ in range(n):
        if rng.random() < 0.25:
            parts.append(f"{rng.choice(VERBS)}{rng.choice(TOKENS)}{rng.choice(ENDINGS)}")
        else:
            parts.append(f"{rng.choice(VERBS)}{rng.choice(NOUNS)}，{rng.choice(ENDINGS)}")
    return "；".join(parts) + "。"


def bullet(rng: random.Random) -> str:
    if rng.random() < 0.3:
        return f"{rng.choice(VERBS)}{rng.choice(TOKENS)} {rng.choice(OBJECTS)}"
    return f"{rng.choice(VERBS)}{rng.choice(OBJECTS)}"


def make_session_id(rng: random.Random, seed_text: str) -> str:
    digest = hashlib.sha1(seed_text.encode()).hexdigest()
    ts = int((BASE_DATE.toordinal() + rng.randrange(SPAN_DAYS)) * 86400 + rng.randrange(86400))
    return f"{ts & 0xFFFFFFFF:08x}{digest[:16]}"


def build_record(rng: random.Random, sid: str, ts: str, with_meta: bool) -> dict:
    rec = {
        "message_id": hashlib.sha1(f"{sid}:{ts}:{rng.random()}".encode()).hexdigest()[:24],
        "message_summary_time": ts,
        "intent": sentence(rng, rng.randint(1, 2)),
        "outcome": sentence(rng, rng.randint(1, 3)),
        "actions": [bullet(rng) for _ in range(rng.randint(2, 4))],
        "learned": [bullet(rng) for _ in range(rng.randint(1, 2))],
    }
    if with_meta:
        rec["compact_summary_meta"] = {
            "created_at_ms": int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() * 1000),
            "mode": rng.choice(["auto", "manual"]),
            "trigger": rng.choice(["token_budget", "turn_count", "user_request"]),
            "server_history_id": hashlib.sha1(ts.encode()).hexdigest()[:32],
            "summary_digest": hashlib.sha1(f"{sid}{ts}".encode()).hexdigest(),
        }
    return rec


def write_text(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录，将被整体重建")
    ap.add_argument("--target-mb", type=float, default=27.0)
    ap.add_argument("--seed", type=int, default=20261104)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(args.out).resolve()
    if out.exists():
        if not args.force:
            print(f"目录已存在，跳过生成：{out}")
            return 0
        shutil.rmtree(out)

    rng = random.Random(args.seed)
    projects_root = out / "projects"
    project_names = [encode_project(d, seg) for d, seg in PROJECTS]
    split_names = {
        name: suffixed(name, i)
        for i, name in enumerate(project_names)
        if i in SPLIT_PROJECTS
    }

    target_bytes = int(args.target_mb * 1024 * 1024 * 0.97)
    total = 0
    stats = {
        "sessions": 0,
        "topics_records": 0,
        "jsonl_files": 0,
        "jsonl_records": 0,
        "dup_jsonl_copies": 0,
        "files_with_meta": 0,
        "corrupt_lines": 0,
    }
    topic_cache: dict[Path, list[str]] = {}
    idx = 0

    while total < target_bytes:
        idx += 1
        pname = rng.choice(project_names)
        day = BASE_DATE + timedelta(days=rng.randrange(SPAN_DAYS))
        day_dir = day.strftime("%Y%m%d")
        sid = make_session_id(rng, f"{pname}|{day_dir}|{idx}")
        session_dir = pname
        if pname in split_names and rng.random() < 0.4:
            session_dir = split_names[pname]

        has_jsonl = rng.random() < 0.85
        has_topics = rng.random() < (0.62 if has_jsonl else 0.15)
        if not has_jsonl and not has_topics:
            has_jsonl = True

        base_dt = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=rng.randrange(8, 23), minutes=rng.randrange(60), seconds=rng.randrange(60)
        )
        topic_lines: list[str] = []
        if has_topics:
            n_topic = rng.choices([1, 2, 3], weights=[35, 45, 20])[0]
            for k in range(n_topic):
                t = base_dt + timedelta(minutes=17 * k + rng.randrange(5))
                body = sentence(rng, rng.randint(2, 4))
                topic_lines.append(
                    f"[session_id: {sid} | topic_summary_time: {t:%Y-%m-%d %H:%M:%S}]{body}\n"
                )
            path = projects_root / session_dir / day_dir / "topics.md"
            buf = topic_cache.setdefault(path, [])
            buf.extend(topic_lines)
            stats["topics_records"] += n_topic
            stats["sessions"] += 1

        if has_jsonl:
            n_lines = rng.choices(
                list(range(1, 25)),
                weights=[max(1, int(24 / (i + 1.5) ** 1.4)) for i in range(1, 25)],
            )[0]
            if idx == 777:
                n_lines = max(n_lines, 3)  # 保证损坏样本落在有多行的文件上
            with_meta = rng.random() < 0.47
            if with_meta:
                stats["files_with_meta"] += 1
            lines: list[str] = []
            for k in range(n_lines):
                ts = (base_dt + timedelta(minutes=rng.randrange(3, 90))).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(json.dumps(build_record(rng, sid, ts, with_meta), ensure_ascii=False))
            if idx == 777:
                lines[1] = lines[1][: len(lines[1]) // 3]  # 截断一条，模拟损坏记录
                stats["corrupt_lines"] += 1
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            path = projects_root / session_dir / day_dir / f"session_memory_{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            total += len(payload)
            stats["jsonl_files"] += 1
            stats["jsonl_records"] += n_lines
            stats["sessions"] += 0 if has_topics else 1
            if pname in split_names and rng.random() < 0.15:
                other = split_names[pname] if session_dir == pname else pname
                dup = projects_root / other / day_dir / path.name
                dup.parent.mkdir(parents=True, exist_ok=True)
                dup.write_bytes(payload)
                stats["dup_jsonl_copies"] += 1

        if idx % 64 == 0:
            for path, buf in topic_cache.items():
                if buf:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8", newline="") as fh:
                        fh.write("".join(buf))
                    total += sum(len(x.encode("utf-8")) for x in buf)
                    buf.clear()

    for path, buf in topic_cache.items():
        if buf:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as fh:
                fh.write("".join(buf))
            total += sum(len(x.encode("utf-8")) for x in buf)
            buf.clear()

    # 非会话文件：仅用于验证适配器不会把它们纳入导入
    for name in project_names:
        pm = projects_root / name / "project_memory.md"
        body = "# 项目记忆\n\n" + "\n".join(f"- {bullet(rng)}" for _ in range(60)) + "\n"
        total += write_text(pm, body)
    total += write_text(out / "user_profile.md", "# 用户画像\n\n" + "\n".join(f"- {bullet(rng)}" for _ in range(40)) + "\n")

    files = [p for p in out.rglob("*") if p.is_file()]
    actual = sum(p.stat().st_size for p in files)
    stats.update(
        {
            "out": str(out),
            "bytes": actual,
            "mb": round(actual / 1024 / 1024, 2),
            "files": len(files),
            "project_dirs": len(list(projects_root.iterdir())),
            "project_bases": len(project_names),
        }
    )
    (out.parent / "gen_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
