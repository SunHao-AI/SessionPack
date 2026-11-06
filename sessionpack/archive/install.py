"""校验通过的归档包安装进目录库。SQL 全部收敛到 catalog API。"""
from __future__ import annotations

import json
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sessionpack import catalog
from sessionpack.archive.build import MANIFEST_NAME
from sessionpack.archive.verify import ArchiveError, VerifyIssue, verify_archive
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord
from sessionpack.textutil import failure_text


@dataclass(frozen=True)
class ArchiveImportResult:
    archive_id: str
    sessions: int
    messages: int
    sessions_created: int
    messages_inserted: int


def import_archive(conn: sqlite3.Connection, path: Path) -> ArchiveImportResult:
    path = Path(path)
    result = verify_archive(path)
    if not result.ok or result.manifest is None:
        raise ArchiveError(result.issues)
    manifest = result.manifest
    project_meta = manifest.get("project")
    if not isinstance(project_meta, dict) or not isinstance(
        project_meta.get("project_id"), str
    ):
        raise ArchiveError([
            VerifyIssue("E_MANIFEST_INVALID", "manifest.project 缺失或非法")
        ])
    project = manifest["project"]
    metas: list[tuple[str, dict]] = []
    illegal: list[VerifyIssue] = []
    with zipfile.ZipFile(path) as bundle:
        for entry in manifest["files"]:
            name = entry["path"]
            if not name.endswith("/meta.json"):
                continue
            try:
                meta = json.loads(bundle.read(name).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                illegal.append(VerifyIssue("E_META_INVALID", name))
                continue
            if not isinstance(meta, dict):
                illegal.append(VerifyIssue("E_META_INVALID", name))
                continue
            session_id = meta.get("session_id")
            if not isinstance(session_id, str) or not catalog.SESSION_ID_RE.fullmatch(
                session_id
            ):
                illegal.append(VerifyIssue("E_SESSION_ID", name))
                continue
            metas.append((name, meta))
    if illegal:
        raise ArchiveError(illegal)
    catalog.upsert_project(conn, ProjectRecord(
        project_id=project["project_id"],
        display_name=project.get("display_name") or project["project_id"],
        local_path_hint=None,
        source_kind=project.get("source_kind") or "archive",
    ))
    run_id = uuid.uuid4().hex
    catalog.begin_import_run(
        conn, run_id, f"archive:{manifest['archive_id']}",
        manifest["format_version"], datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )
    conn.commit()
    current = MANIFEST_NAME
    try:
        sessions = messages = created = inserted = 0
        touched: set[str] = set()
        with zipfile.ZipFile(path) as bundle:
            for name, meta in metas:
                current = name
                session_id = meta["session_id"]
                session_dir = name.rsplit("/", 1)[0]
                touched.add(session_id)
                summary = meta.get("summary") or meta.get("title") or ""
                if summary:
                    outcome = catalog.upsert_topic(conn, TopicRecord(
                        session_id=session_id,
                        topic_time=meta.get("updated_at") or meta.get("created_at") or "",
                        body=summary,
                        project_id=meta["project_id"],
                        source_kind=meta.get("source_kind") or "archive",
                    ))
                    created += int(outcome.session_created)
                sessions += 1
                current = f"{session_dir}/messages.ndjson"
                rows = bundle.read(current).decode("utf-8")
                for line in rows.splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    outcome = catalog.upsert_message(conn, MessageRecord(
                        session_id=session_id,
                        message_id=payload["message_id"],
                        seq=int(payload.get("seq") or 0),
                        ts=payload.get("ts"),
                        intent=payload.get("intent"),
                        outcome=payload.get("outcome"),
                        actions=tuple(payload.get("actions") or ()),
                        learned=tuple(payload.get("learned") or ()),
                        meta=payload.get("meta"),
                        body=payload.get("body") or "",
                        project_id=meta["project_id"],
                        source_kind=meta.get("source_kind") or "archive",
                    ))
                    inserted += int(outcome.inserted)
                    created += int(outcome.session_created)
                    messages += 1
        if touched:
            catalog.refresh_session_hashes(conn, touched)
        catalog.finish_import_run(
            conn, run_id, finished_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            result="ok", scanned_files=len(manifest["files"]),
            imported_rows=inserted, failed_rows=0,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        catalog.finish_import_run(
            conn, run_id, finished_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            result="failed",
            warnings_json=json.dumps({"error": f"{current}: {failure_text(exc)}"},
                                     ensure_ascii=False),
        )
        conn.commit()
        raise ArchiveError(
            [VerifyIssue("E_IMPORT_FAILED", f"{current}: {failure_text(exc)}")]) from exc
    return ArchiveImportResult(
        archive_id=manifest["archive_id"],
        sessions=sessions,
        messages=messages,
        sessions_created=created,
        messages_inserted=inserted,
    )
