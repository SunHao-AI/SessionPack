from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FileKind = Literal["topics", "messages"]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    local_path_hint: str | None
    source_kind: str


@dataclass(frozen=True)
class TopicRecord:
    session_id: str
    topic_time: str
    body: str
    project_id: str
    source_kind: str


@dataclass(frozen=True)
class MessageRecord:
    session_id: str
    message_id: str
    seq: int
    ts: str | None
    intent: str | None
    outcome: str | None
    actions: tuple[str, ...]
    learned: tuple[str, ...]
    meta: dict | None
    body: str
    project_id: str
    source_kind: str


Record = TopicRecord | MessageRecord


@dataclass(frozen=True)
class SourceFile:
    path: Path
    project_id: str
    raw_project_dir: str
    kind: FileKind


@dataclass(frozen=True)
class InspectResult:
    adapter_id: str
    adapter_version: int
    source_kind: str
    root: Path
    project_count: int
    project_dir_count: int
    topic_files: int
    jsonl_files: int
    bytes_total: int
    date_min: str | None
    date_max: str | None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_kind": self.source_kind,
            "root": str(self.root),
            "projects": self.project_count,
            "project_dirs": self.project_dir_count,
            "topic_files": self.topic_files,
            "jsonl_files": self.jsonl_files,
            "bytes_total": self.bytes_total,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "notes": list(self.notes),
        }
