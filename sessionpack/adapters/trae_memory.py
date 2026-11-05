from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from sessionpack.model import (
    InspectResult,
    MessageRecord,
    Record,
    SourceFile,
    TopicRecord,
)

TOPIC_RE = re.compile(
    r"^\[session_id: ([0-9a-f]{24}) \| topic_summary_time: "
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\](.*)$"
)
SUFFIX_RE = re.compile(r"--p\d+-[0-9a-f]{16}$")
SESSION_FILE_RE = re.compile(r"^session_memory_([0-9a-f]{24})\.jsonl$")


def canonical_project_dir(name: str) -> str:
    return SUFFIX_RE.sub("", name)


def project_display_name(canonical: str) -> str:
    parts = canonical.lstrip("-").split("-")
    if len(parts) < 2:
        return canonical
    return f"{parts[0].upper()}:\\" + "\\".join(parts[1:])


class TraeMemoryAdapter:
    id = "trae-cn-memory"
    version = 1
    source_kind = "trae-memory"

    def detect(self, root: Path) -> bool:
        return root.is_dir() and (root / "projects").is_dir()

    def declared_paths(self, root: Path) -> list[Path]:
        declared: list[Path] = []
        profile = root / "user_profile.md"
        if profile.is_file():
            declared.append(profile)
        projects = root / "projects"
        if not projects.is_dir():
            return declared
        for project in sorted(projects.iterdir()):
            if not project.is_dir():
                continue
            memory = project / "project_memory.md"
            if memory.is_file():
                declared.append(memory)
            for day in sorted(project.iterdir()):
                if not day.is_dir():
                    continue
                topics = day / "topics.md"
                if topics.is_file():
                    declared.append(topics)
                for candidate in sorted(day.iterdir()):
                    if candidate.is_file() and SESSION_FILE_RE.match(candidate.name):
                        declared.append(candidate)
        return declared

    def enumerate(self, root: Path) -> Iterator[SourceFile]:
        for path in self.declared_paths(root):
            if path.name == "topics.md":
                raw = path.parent.parent.name
                yield SourceFile(path, canonical_project_dir(raw), raw, "topics")
            elif SESSION_FILE_RE.match(path.name):
                raw = path.parent.parent.name
                yield SourceFile(path, canonical_project_dir(raw), raw, "messages")

    def inspect(self, root: Path) -> InspectResult:
        declared = self.declared_paths(root)
        sources = [f for f in self.enumerate(root)]
        topics = [f for f in sources if f.kind == "topics"]
        jsonls = [f for f in sources if f.kind == "messages"]
        total = 0
        for path in declared:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        days = sorted(
            path.parent.name
            for path in declared
            if path.parent.name.isdigit() and len(path.parent.name) == 8
        )
        dates = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
        notes: list[str] = []
        merged = {f.project_id for f in sources}
        if len({f.raw_project_dir for f in sources}) > len(merged):
            notes.append("已按 --pN-<hash> 后缀归并同名项目目录")
        return InspectResult(
            adapter_id=self.id,
            adapter_version=self.version,
            source_kind=self.source_kind,
            root=root,
            project_count=len(merged),
            project_dir_count=len({f.raw_project_dir for f in sources}),
            topic_files=len(topics),
            jsonl_files=len(jsonls),
            bytes_total=total,
            date_min=dates[0] if dates else None,
            date_max=dates[-1] if dates else None,
            notes=tuple(notes),
        )

    def _session_id(self, ref: SourceFile) -> str:
        match = SESSION_FILE_RE.match(ref.path.name)
        if match is None:
            raise ValueError("invalid session file name")
        return match.group(1)

    def read_file(
        self, ref: SourceFile, start_offset: int = 0, start_seq: int = 0
    ) -> Iterator[tuple[int, Record | None, str | None]]:
        session_id = self._session_id(ref) if ref.kind == "messages" else None
        offset = start_offset
        seq = start_seq
        with ref.path.open("rb") as handle:
            handle.seek(start_offset)
            for raw in handle:
                offset += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    yield offset, None, None
                    continue
                if ref.kind == "topics":
                    match = TOPIC_RE.match(line)
                    if match is None:
                        yield offset, None, "topic format mismatch"
                        continue
                    yield offset, TopicRecord(
                        session_id=match.group(1),
                        topic_time=match.group(2),
                        body=match.group(3).strip(),
                        project_id=ref.project_id,
                        source_kind=self.source_kind,
                    ), None
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                    message_id = payload.get("message_id")
                    if not isinstance(message_id, str) or not message_id:
                        raise ValueError("missing message_id")
                    actions = payload.get("actions")
                    learned = payload.get("learned")
                    actions = [x for x in actions if isinstance(x, str)] if isinstance(actions, list) else []
                    learned = [x for x in learned if isinstance(x, str)] if isinstance(learned, list) else []
                    intent = payload.get("intent") if isinstance(payload.get("intent"), str) else None
                    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), str) else None
                    ts = payload.get("message_summary_time")
                    ts = ts if isinstance(ts, str) else None
                    meta = payload.get("compact_summary_meta")
                    meta = meta if isinstance(meta, dict) else None
                except (ValueError, json.JSONDecodeError) as exc:
                    yield offset, None, str(exc)[:60]
                    continue
                seq += 1
                body = " ".join([x for x in [intent, outcome, *actions, *learned] if x])
                yield offset, MessageRecord(
                    session_id=session_id,
                    message_id=message_id,
                    seq=seq,
                    ts=ts,
                    intent=intent,
                    outcome=outcome,
                    actions=tuple(actions),
                    learned=tuple(learned),
                    meta=meta,
                    body=body,
                    project_id=ref.project_id,
                    source_kind=self.source_kind,
                ), None
