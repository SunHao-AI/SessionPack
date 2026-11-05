#!/usr/bin/env python3
"""流式导入 Trae 记忆目录到本地 SQLite 索引。

原型只实现压测需要的部分：声明路径白名单、逐行流式解析、
按 session_id 聚合与项目目录归并、幂等写入、按文件偏移断点续传。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

TOPIC_RE = re.compile(
    r"^\[session_id: ([0-9a-f]{24}) \| topic_summary_time: "
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\](.*)$"
)
SUFFIX_RE = re.compile(r"--p\d+-[0-9a-f]{16}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id   TEXT PRIMARY KEY,
  dir_name     TEXT NOT NULL,
  display_name TEXT,
  dir_names    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL,
  title        TEXT,
  summary      TEXT,
  created_at   TEXT,
  updated_at   TEXT
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
CREATE TABLE IF NOT EXISTS topic_revisions (
  session_id TEXT NOT NULL,
  topic_time TEXT NOT NULL,
  body       TEXT,
  PRIMARY KEY (session_id, topic_time)
);
CREATE TABLE IF NOT EXISTS import_files (
  path    TEXT PRIMARY KEY,
  size    INTEGER NOT NULL,
  mtime   REAL NOT NULL,
  offset  INTEGER NOT NULL DEFAULT 0,
  rows    INTEGER NOT NULL DEFAULT 0,
  done    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_runs (
  run_id     TEXT PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  result     TEXT,
  warnings   TEXT
);
"""


class PeakRSS:
    """采样当前进程峰值 RSS，Windows 走 psapi，其它平台退回 resource。"""

    def __init__(self) -> None:
        self.peak = 0
        self._impl = self._init_impl()

    def _init_impl(self):
        if os.name == "nt":
            from ctypes import wintypes

            class PMC(ctypes.Structure):
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

            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            # K32GetProcessMemoryInfo 在 Win7+ 的 kernel32 中导出，优先使用；
            # 两条路径都必须显式声明 argtypes，否则句柄被截断而恒返回 0。
            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if get_info is None:
                get_info = psapi.GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            get_info.restype = wintypes.BOOL

            def sample() -> int:
                handle = kernel32.GetCurrentProcess()
                if get_info(handle, ctypes.byref(counters), counters.cb):
                    return counters.PeakWorkingSetSize
                return 0

            return sample

        import resource

        def sample() -> int:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

        return sample

    def touch(self) -> int:
        self.peak = max(self.peak, self._impl())
        return self.peak

    @property
    def mb(self) -> float:
        return round(self.peak / 1024 / 1024, 1)


def canonical_dir(name: str) -> str:
    return SUFFIX_RE.sub("", name)


def display_name(dir_name: str) -> str:
    parts = dir_name.lstrip("-").split("-")
    if len(parts) < 2:
        return dir_name
    drive = parts[0].upper()
    return f"{drive}:\\" + "\\".join(parts[1:])


def declare_paths(memory_root: Path) -> list[Path]:
    """按目录结构声明将要打开的文件清单，不打开任何非清单文件。"""
    declared: list[Path] = []
    projects = memory_root / "projects"
    if not projects.is_dir():
        return declared
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        for day in sorted(proj.iterdir()):
            if not day.is_dir():
                continue
            topics = day / "topics.md"
            if topics.is_file():
                declared.append(topics)
            for f in sorted(day.glob("session_memory_*.jsonl")):
                if f.is_file():
                    declared.append(f)
    return declared


def init_db(conn: sqlite3.Connection) -> str:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_fts'"
    ).fetchone()
    if row:
        return "trigram" if "trigram" in (row[0] or "") else "unicode61"
    for tokenizer in ("trigram", "unicode61"):
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE message_fts USING fts5("
                f"session_id UNINDEXED, message_id UNINDEXED, text, tokenize='{tokenizer}')"
            )
            conn.commit()
            return tokenizer
        except sqlite3.OperationalError:
            continue
    conn.commit()
    return "none"


class Writer:
    def __init__(self, conn: sqlite3.Connection, batch: int, crash_after_rows: int | None,
                 root: Path | None = None):
        self.conn = conn
        self.batch = batch
        self.crash_after_rows = crash_after_rows
        self.root = root
        self.pending: list[tuple] = []
        self.cur_key: str | None = None
        self.cur_pos = 0
        self.stats = {
            "rows_read": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "rows_failed": 0,
            "sessions_new": 0,
            "failures": [],
        }
        self.rss = PeakRSS()
        self.batches = 0

    def add(self, row: tuple) -> None:
        self.pending.append(row)
        self.stats["rows_read"] += 1
        if self.crash_after_rows and self.stats["rows_read"] >= self.crash_after_rows:
            # 模拟进程在批次中途被强杀：本批未提交的数据随事务回滚
            os._exit(9)
        if len(self.pending) >= self.batch:
            self.flush()

    def begin_file(self, key: str) -> None:
        self.cur_key = key
        self.cur_pos = 0

    def end_file(self) -> None:
        self.cur_key = None

    def flush(self) -> None:
        if not self.pending:
            return
        conn = self.conn
        n_rows = len(self.pending)
        for kind, payload in self.pending:
            if kind == "topic":
                sid, ts, body, pid = payload
                cur = conn.execute(
                    "INSERT OR IGNORE INTO topic_revisions(session_id, topic_time, body) VALUES(?,?,?)",
                    (sid, ts, body),
                )
                if cur.rowcount:
                    self.stats["rows_inserted"] += 1
                else:
                    self.stats["rows_skipped"] += 1
                pid = payload[3]
                new = conn.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, project_id, title, summary,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (sid, pid, body[:60], body, ts, ts),
                ).rowcount
                if new:
                    self.stats["sessions_new"] += 1
                conn.execute(
                    "UPDATE sessions SET title=?, summary=?, updated_at=? "
                    "WHERE session_id=? AND (updated_at IS NULL OR updated_at < ?)",
                    (body[:60], body, ts, sid, ts),
                )
            else:
                sid, mid, seq, ts, intent, outcome, actions, learned, meta, body, pid = payload
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages(session_id, message_id, seq, ts, intent,"
                    " outcome, actions, learned, meta_json, body) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, mid, seq, ts, intent, outcome, actions, learned, meta, body),
                )
                if cur.rowcount:
                    self.stats["rows_inserted"] += 1
                    conn.execute(
                        "INSERT INTO message_fts(session_id, message_id, text) VALUES(?,?,?)",
                        (sid, mid, body),
                    )
                else:
                    self.stats["rows_skipped"] += 1
                title = (intent or "")[:60] or None
                new = conn.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, project_id, title, created_at,"
                    " updated_at) VALUES(?,?,?,?,?)",
                    (sid, pid, title, ts, ts),
                ).rowcount
                if new:
                    self.stats["sessions_new"] += 1
                conn.execute(
                    "UPDATE sessions SET updated_at=? "
                    "WHERE session_id=? AND (updated_at IS NULL OR updated_at < ?)",
                    (ts, sid, ts),
                )
        self.pending.clear()
        self.batches += 1
        if self.cur_key:
            # 已写入行与字节偏移在同一事务内提交，断点位置不会领先于数据
            conn.execute(
                "UPDATE import_files SET offset=?, rows=rows+? WHERE path=?",
                (self.cur_pos, n_rows, self.cur_key),
            )
        self.rss.touch()
        conn.commit()

    def fail(self, where: str, reason: str) -> None:
        self.stats["rows_failed"] += 1
        if len(self.stats["failures"]) < 20:
            self.stats["failures"].append({"where": where, "reason": reason})

    def rel(self, path: Path) -> str:
        """失败记录使用相对路径，避免同一仓库的两份目录副本无法区分。"""
        try:
            return path.relative_to(self.root).as_posix() if self.root else path.name
        except ValueError:
            return path.as_posix()


def project_rows(conn: sqlite3.Connection, project_id: str, dir_name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO projects(project_id, dir_name, display_name, dir_names)"
        " VALUES(?,?,?,?)",
        (project_id, canonical_dir(dir_name), display_name(canonical_dir(dir_name)), dir_name),
    )


def process_jsonl(writer: Writer, path: Path, project_id: str, start_offset: int,
                  counters: dict) -> int:
    offset = start_offset
    sid = path.stem[len("session_memory_"):]
    seq = 0
    with path.open("rb") as fh:
        fh.seek(start_offset)
        for raw in fh:
            offset += len(raw)
            writer.cur_pos = offset
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
                mid = rec.get("message_id")
                if not isinstance(mid, str) or not mid:
                    raise ValueError("missing message_id")
                actions = rec.get("actions") or []
                learned = rec.get("learned") or []
                if not isinstance(actions, list):
                    actions = []
                if not isinstance(learned, list):
                    learned = []
                actions = [x for x in actions if isinstance(x, str)]
                learned = [x for x in learned if isinstance(x, str)]
                intent = rec.get("intent") if isinstance(rec.get("intent"), str) else None
                outcome = rec.get("outcome") if isinstance(rec.get("outcome"), str) else None
                ts = rec.get("message_summary_time")
                ts = ts if isinstance(ts, str) else None
                meta = rec.get("compact_summary_meta")
                meta_json = json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else None
            except (ValueError, json.JSONDecodeError) as exc:
                writer.fail(f"{writer.rel(path)}:{offset}", str(exc)[:60])
                continue
            seq += 1
            body = " ".join([x for x in [intent, outcome, *actions, *learned] if x])
            writer.add(("msg", (sid, mid, seq, ts, intent, outcome,
                                "|".join(actions), "|".join(learned), meta_json, body, project_id)))
    counters["jsonl_files"] += 1
    return offset


def process_topics(writer: Writer, path: Path, project_id: str, start_offset: int,
                   counters: dict) -> int:
    offset = start_offset
    with path.open("rb") as fh:
        fh.seek(start_offset)
        for raw in fh:
            offset += len(raw)
            writer.cur_pos = offset
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            m = TOPIC_RE.match(line)
            if not m:
                writer.fail(f"{writer.rel(path)}:{offset}", "topic format mismatch")
                continue
            sid, ts, body = m.group(1), m.group(2), m.group(3).strip()
            writer.add(("topic", (sid, ts, body, project_id)))
    counters["topic_files"] += 1
    return offset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-root", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--crash-after-rows", type=int, default=None)
    ap.add_argument("--reparse", action="store_true", help="忽略已完成标记，从头重扫以验证去重")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    memory_root = Path(args.memory_root).resolve()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    declared = declare_paths(memory_root)
    declared_set = {os.path.normcase(os.path.abspath(p)) for p in declared}

    opened: set[str] = set()

    def audit(event: str, hook_args: tuple) -> None:
        if event == "open" and hook_args and isinstance(hook_args[0], (str, bytes, os.PathLike)):
            try:
                opened.add(os.path.normcase(os.path.abspath(os.fspath(hook_args[0]))))
            except (TypeError, ValueError):
                pass

    sys.addaudithook(audit)

    conn = sqlite3.connect(db_path)
    tokenizer = init_db(conn)
    writer = Writer(conn, args.batch, args.crash_after_rows, memory_root)
    counters = {"jsonl_files": 0, "topic_files": 0, "resumed_files": 0, "skipped_files": 0}
    run_id = f"run-{int(time.time() * 1000)}"
    conn.execute(
        "INSERT INTO import_runs(run_id, started_at, result) VALUES(?,?,?)",
        (run_id, time.strftime("%Y-%m-%d %H:%M:%S"), "running"),
    )
    conn.commit()

    projects_seen: dict[str, str] = {}
    for path in declared:
        project_dir = canonical_dir(path.parent.parent.name)
        raw_dir = path.parent.parent.name
        if project_dir not in projects_seen:
            project_rows(conn, project_dir, raw_dir)
            projects_seen[project_dir] = raw_dir
        key = os.path.normcase(os.path.abspath(path))
        stat = path.stat()
        row = conn.execute(
            "SELECT size, mtime, offset, done FROM import_files WHERE path=?", (key,)
        ).fetchone()
        start_offset = 0
        if not args.reparse and row and row[0] == stat.st_size and abs(row[1] - stat.st_mtime) < 1e-6:
            if row[3] == 1:
                counters["skipped_files"] += 1
                continue
            start_offset = row[2]
            counters["resumed_files"] += 1
        conn.execute(
            "INSERT INTO import_files(path, size, mtime, offset, rows, done) VALUES(?,?,?,?,0,0)"
            " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
            " offset=excluded.offset, done=0",
            (key, stat.st_size, stat.st_mtime, start_offset),
        )
        conn.commit()

        try:
            writer.begin_file(key)
            if path.name == "topics.md":
                offset = process_topics(writer, path, project_dir, start_offset, counters)
            else:
                offset = process_jsonl(writer, path, project_dir, start_offset, counters)
        except OSError as exc:
            writer.end_file()
            writer.fail(str(path), f"io {exc.__class__.__name__}")
            continue

        # 批次内已写入的行与偏移一起提交，保证断点与数据一致
        writer.cur_pos = offset
        writer.flush()
        writer.end_file()
        conn.execute(
            "UPDATE import_files SET offset=?, done=1 WHERE path=?", (offset, key)
        )
        conn.commit()
        writer.rss.touch()

    elapsed = time.perf_counter() - started
    totals = {
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "topics": conn.execute("SELECT COUNT(*) FROM topic_revisions").fetchone()[0],
        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
    }
    unexpected = sorted(
        p for p in opened
        if p.startswith(os.path.normcase(str(memory_root))) and p not in declared_set
        and os.path.isfile(p)
    )
    db_size = db_path.stat().st_size + sum(
        Path(str(db_path) + suffix).stat().st_size
        for suffix in ("-wal",)
        if Path(str(db_path) + suffix).exists()
    )
    report = {
        "run_id": run_id,
        "tokenizer": tokenizer,
        "elapsed_s": round(elapsed, 2),
        "declared_files": len(declared),
        "opened_under_source": len([p for p in opened if p.startswith(os.path.normcase(str(memory_root)))]),
        "unexpected_opens": unexpected,
        "files_processed": counters["jsonl_files"] + counters["topic_files"],
        "resumed_files": counters["resumed_files"],
        "skipped_unchanged_files": counters["skipped_files"],
        "rows_read": writer.stats["rows_read"],
        "rows_inserted": writer.stats["rows_inserted"],
        "rows_skipped": writer.stats["rows_skipped"],
        "rows_failed": writer.stats["rows_failed"],
        "sessions_new": writer.stats["sessions_new"],
        "batches": writer.batches,
        "peak_rss_mb": writer.rss.mb,
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "failures": writer.stats["failures"],
        **totals,
    }
    conn.execute(
        "UPDATE import_runs SET finished_at=?, result=? WHERE run_id=?",
        (time.strftime("%Y-%m-%d %H:%M:%S"), "ok", run_id),
    )
    conn.commit()
    conn.close()

    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
