from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters.base import MemoryAdapter
from sessionpack.adapters.trae_memory import project_display_name
from sessionpack.mem import peak_rss_mb
from sessionpack.model import MessageRecord, ProjectRecord, SourceFile, TopicRecord

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
    conn.execute(
        "INSERT INTO import_runs(run_id, adapter_id, adapter_version, started_at, result)"
        " VALUES(?,?,?,?,?)",
        (stats.run_id, adapter.id, adapter.version, _now(), "running"),
    )
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
            display_name=project_display_name(ref.project_id),
            local_path_hint=ref.raw_project_dir,
            source_kind=adapter.source_kind,
        )
    for project in projects.values():
        catalog.upsert_project(conn, project)

    existing = {
        row["path"]: row for row in conn.execute("SELECT * FROM import_files")
    }
    states: dict[str, _FileState] = {}
    to_insert: list[tuple[str, int, float]] = []
    resets: list[tuple[int, float, str]] = []
    for ref in files:
        key = str(ref.path)
        try:
            info = ref.path.stat()
        except OSError as exc:
            stats.rows_failed += 1
            stats.failures.append(f"{_relative(ref.path, root)}: {str(exc)[:60]}")
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

    if to_insert:
        conn.executemany(
            "INSERT OR IGNORE INTO import_files(path, size, mtime) VALUES(?,?,?)",
            to_insert,
        )
    for size, mtime, key in resets:
        conn.execute(
            "UPDATE import_files SET size=?, mtime=?, offset=0, rows=0, done=0,"
            " error=NULL WHERE path=?",
            (size, mtime, key),
        )
    conn.commit()

    dirty: set[str] = set()
    uncommitted = 0

    def commit(force: bool = False) -> None:
        nonlocal uncommitted
        if not uncommitted and not force:
            return
        for key in list(dirty):
            state = states[key]
            conn.execute(
                "UPDATE import_files SET offset=?, rows=?, done=?, last_run_id=?,"
                " error=? WHERE path=?",
                (state.offset, state.rows, 1 if state.done else 0,
                 stats.run_id, state.error, key),
            )
        dirty.clear()
        conn.commit()
        if uncommitted:
            stats.batches += 1
        uncommitted = 0

    touched: set[str] = set()
    stop = False
    for ref in files:
        key = str(ref.path)
        state = states.get(key)
        if state is None:
            continue
        if state.done and not reparse:
            stats.files_skipped += 1
            continue
        start_offset, start_seq = (0, 0) if reparse else (state.offset, state.rows)
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
                uncommitted += 1
                if crash_after_rows is not None and stats.rows_read >= crash_after_rows:
                    os._exit(9)
                if stop_after_rows is not None and stats.rows_read >= stop_after_rows:
                    stop = True
                    break
                if uncommitted >= batch:
                    commit()
        except OSError as exc:
            stats.rows_failed += 1
            state.error = str(exc)[:60]
            state.done = False
            state.dirty = True
            dirty.add(key)
            if len(stats.failures) < MAX_FAILURES:
                stats.failures.append(f"{state.rel}: {str(exc)[:60]}")
        if stop:
            stats.stopped = True
            commit()
            break
        state.done = True
        state.dirty = True
        dirty.add(key)
        stats.files_processed += 1

    commit(force=True)
    if touched:
        catalog.refresh_session_hashes(conn, touched)
    stats.elapsed_s = round(time.perf_counter() - started, 2)
    stats.peak_rss_mb = peak_rss_mb()
    conn.execute(
        "UPDATE import_runs SET finished_at=?, result=?, scanned_files=?,"
        " imported_rows=?, failed_rows=?, warnings_json=? WHERE run_id=?",
        (
            _now(),
            "stopped" if stop else "ok",
            stats.files_declared,
            stats.messages_inserted + stats.topics_applied,
            stats.rows_failed,
            json.dumps(
                {"failures": stats.failures, "tokenizer": catalog.tokenizer_of(conn)},
                ensure_ascii=False,
            ),
            stats.run_id,
        ),
    )
    conn.commit()
    conn.close()
    return stats
