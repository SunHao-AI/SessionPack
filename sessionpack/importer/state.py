"""导入器状态与选择：常量 / ImportStats / 文件状态 / 进度 / 小工具。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from sessionpack.model import SourceFile

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
