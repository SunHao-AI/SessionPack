"""导入前置规划：扫描比对 import_files、批量提交进度。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sessionpack import catalog
from sessionpack.importer.state import ImportStats, _FileState, _relative, _RunProgress
from sessionpack.model import SourceFile


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
