from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters.base import MemoryAdapter
from sessionpack.mem import peak_rss_mb
from sessionpack.model import ProjectRecord, SourceFile, TopicRecord

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


@dataclass
class _RunProgress:
    uncommitted: int = 0


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
    retry_failed: bool = False,
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
    catalog.begin_import_run(
        conn, stats.run_id, adapter.id, adapter.version, _now())
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
            display_name=adapter.display_name(ref.project_id),
            local_path_hint=ref.raw_project_dir,
            source_kind=adapter.source_kind,
        )
    for project in projects.values():
        catalog.upsert_project(conn, project)

    states, dirty, progress = _plan_files(
        conn, files, stats, reparse=reparse, retry_failed=retry_failed, root=root
    )

    touched: set[str] = set()
    stop = False
    for ref in files:
        state = states.get(str(ref.path))
        if state is None:
            continue
        if _process_file(
            conn, adapter, ref, state, stats,
            batch=batch, reparse=reparse, retry_failed=retry_failed,
            crash_after_rows=crash_after_rows, stop_after_rows=stop_after_rows,
            touched=touched, dirty=dirty, states=states, progress=progress,
        ):
            stop = True
            break

    _finish_run(conn, stats, states, dirty, progress,
                touched=touched, stop=stop, started=started)
    conn.close()
    return stats


def _plan_files(
    conn: sqlite3.Connection,
    files: list[SourceFile],
    stats: ImportStats,
    *,
    reparse: bool,
    retry_failed: bool,
    root: Path,
) -> tuple[dict[str, _FileState], set[str], _RunProgress]:
    """扫描/比对 import_files，返回 (states, dirty, progress) 前置状态。"""
    existing = catalog.load_import_files(conn)
    states: dict[str, _FileState] = {}
    to_insert: list[tuple[str, int, float]] = []
    resets: list[tuple[int, float, str]] = []
    for ref in files:
        key = str(ref.path)
        try:
            info = ref.path.stat()
        except OSError as exc:
            stats.rows_failed += 1
            text = (exc.strerror or str(exc))[:60]
            stats.failures.append(f"{_relative(ref.path, root)}: {text}")
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
        if retry_failed and row["error"]:
            catalog.clear_import_file_error(conn, row["path"])
            states[key].offset = 0
            states[key].rows = 0
            states[key].done = False
            states[key].error = None

    if to_insert:
        catalog.register_import_files(conn, to_insert)
    for size, mtime, key in resets:
        catalog.reset_import_file(conn, size=size, mtime=mtime, path=key)
    conn.commit()

    dirty: set[str] = set()
    progress = _RunProgress()
    return states, dirty, progress


def _commit_progress(
    conn: sqlite3.Connection,
    stats: ImportStats,
    states: dict[str, _FileState],
    dirty: set[str],
    progress: _RunProgress,
    force: bool = False,
) -> None:
    if not progress.uncommitted and not force:
        return
    for key in list(dirty):
        state = states[key]
        catalog.save_import_file_state(
            conn, path=key, offset=state.offset, rows=state.rows,
            done=1 if state.done else 0, last_run_id=stats.run_id,
            error=state.error,
        )
    dirty.clear()
    conn.commit()
    if progress.uncommitted:
        stats.batches += 1
    progress.uncommitted = 0


def _process_file(
    conn: sqlite3.Connection,
    adapter: MemoryAdapter,
    ref: SourceFile,
    state: _FileState,
    stats: ImportStats,
    *,
    batch: int,
    reparse: bool,
    retry_failed: bool,
    crash_after_rows: int | None,
    stop_after_rows: int | None,
    touched: set[str],
    dirty: set[str],
    states: dict[str, _FileState],
    progress: _RunProgress,
) -> bool:
    """搬运原主循环单文件体；返回 True 表示应停止外层循环。"""
    key = str(ref.path)
    if state.done and not reparse:
        stats.files_skipped += 1
        if ref.session_id:
            touched.add(ref.session_id)
        return False
    start_offset, start_seq = (0, 0) if reparse else (state.offset, state.rows)
    stop = False
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
            progress.uncommitted += 1
            if crash_after_rows is not None and stats.rows_read >= crash_after_rows:
                os._exit(9)
            if stop_after_rows is not None and stats.rows_read >= stop_after_rows:
                stop = True
                break
            if progress.uncommitted >= batch:
                _commit_progress(conn, stats, states, dirty, progress)
    except OSError as exc:
        stats.rows_failed += 1
        text = (exc.strerror or str(exc))[:60]
        state.error = text
        state.done = False
        state.dirty = True
        dirty.add(key)
        if len(stats.failures) < MAX_FAILURES:
            stats.failures.append(f"{state.rel}: {text}")
    else:
        if stop:
            stats.stopped = True
            _commit_progress(conn, stats, states, dirty, progress)
            return True
        state.done = True
        state.dirty = True
        if retry_failed:
            state.error = None
    dirty.add(key)
    stats.files_processed += 1
    return False


def _finish_run(
    conn: sqlite3.Connection,
    stats: ImportStats,
    states: dict[str, _FileState],
    dirty: set[str],
    progress: _RunProgress,
    *,
    touched: set[str],
    stop: bool,
    started: float,
) -> None:
    _commit_progress(conn, stats, states, dirty, progress, force=True)
    if touched:
        catalog.refresh_session_hashes(conn, touched)
    stats.elapsed_s = round(time.perf_counter() - started, 2)
    stats.peak_rss_mb = peak_rss_mb()
    catalog.finish_import_run(
        conn, stats.run_id,
        finished_at=_now(),
        result="stopped" if stop else "ok",
        scanned_files=stats.files_declared,
        imported_rows=stats.messages_inserted + stats.topics_applied,
        failed_rows=stats.rows_failed,
        warnings_json=json.dumps(
            {"failures": stats.failures, "tokenizer": catalog.tokenizer_of(conn)},
            ensure_ascii=False,
        ),
    )
    conn.commit()
