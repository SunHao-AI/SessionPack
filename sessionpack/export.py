from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sessionpack import catalog
from sessionpack.summary import build_summary, render_markdown, split_list


@dataclass(frozen=True)
class ExportResult:
    paths: tuple[Path, ...]
    sessions: int
    messages: int


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _message_payload(message: dict) -> dict:
    return {
        "message_id": message["message_id"],
        "seq": message["seq"],
        "ts": message.get("ts"),
        "intent": message.get("intent"),
        "outcome": message.get("outcome"),
        "actions": split_list(message.get("actions")),
        "learned": split_list(message.get("learned")),
        "body": message.get("body") or "",
    }


def _render(session: dict, messages: list[dict], fmt: str) -> str:
    if fmt == "json":
        payload = {
            "summary": build_summary(session, messages),
            "messages": [_message_payload(message) for message in messages],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if fmt == "markdown":
        return render_markdown(session, messages)
    raise ValueError(f"不支持的导出格式: {fmt}")


def export_session(
    conn: sqlite3.Connection, session_id: str, dest_dir: Path, fmt: str = "markdown"
) -> Path:
    session = catalog.get_session(conn, session_id)
    if session is None:
        raise KeyError(session_id)
    messages = catalog.get_messages(conn, session_id)
    suffix = "json" if fmt == "json" else "md"
    path = Path(dest_dir) / f"{session_id}.{suffix}"
    _atomic_write(path, _render(session, messages, fmt))
    return path


def _export_rows(conn: sqlite3.Connection, rows: list[dict], dest_dir: Path,
                 fmt: str) -> ExportResult:
    paths: list[Path] = []
    messages = 0
    for row in rows:
        paths.append(export_session(conn, row["session_id"], dest_dir, fmt))
        messages += len(catalog.get_messages(conn, row["session_id"]))
    return ExportResult(tuple(paths), len(paths), messages)


def export_project(
    conn: sqlite3.Connection, project_id: str, dest_dir: Path, fmt: str = "markdown"
) -> ExportResult:
    rows = catalog.list_sessions(conn, project_id=project_id, limit=100000)
    return _export_rows(conn, rows, dest_dir, fmt)


def export_all(
    conn: sqlite3.Connection, dest_dir: Path, fmt: str = "markdown"
) -> ExportResult:
    rows = catalog.list_sessions(conn, limit=100000)
    return _export_rows(conn, rows, dest_dir, fmt)
