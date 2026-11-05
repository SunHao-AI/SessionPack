from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sessionpack import __version__, catalog
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord

FORMAT_NAME = "sessionpack-archive"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    archive_id: str
    project_id: str
    sessions: int
    messages: int
    bytes: int


@dataclass(frozen=True)
class VerifyIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class VerifyResult:
    path: Path
    ok: bool
    issues: tuple[VerifyIssue, ...]
    manifest: dict | None


@dataclass(frozen=True)
class ArchiveImportResult:
    archive_id: str
    sessions: int
    messages: int
    sessions_created: int
    messages_inserted: int


class ArchiveError(RuntimeError):
    def __init__(self, issues: tuple[VerifyIssue, ...] | list[VerifyIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.code}: {i.detail}" for i in self.issues))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _slug(text: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in text)
    cleaned = cleaned.strip("-")[:60]
    return cleaned or "project"


def _unsafe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return True
    pure = PurePosixPath(name)
    return pure.is_absolute() or ".." in pure.parts


def compute_archive_id(files: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(files, key=lambda item: item["path"]):
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_archive(
    conn: sqlite3.Connection,
    out_dir: Path,
    project_id: str,
    *,
    now: str | None = None,
) -> ArchiveResult:
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE project_id=? ORDER BY session_id", (project_id,)
    ).fetchall()
    if not sessions:
        raise ValueError(f"项目没有可归档的会话: {project_id}")
    project_row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    created_at = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=out_dir))
    try:
        entries: list[dict] = []
        total_messages = 0
        for session in sessions:
            session_id = session["session_id"]
            messages = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY seq, message_id",
                (session_id,),
            ).fetchall()
            total_messages += len(messages)
            session_dir = work / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "session_id": session_id,
                "project_id": session["project_id"],
                "source_kind": session["source_kind"],
                "title": session["title"],
                "summary": session["summary"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "content_hash": session["content_hash"],
                "status": session["status"],
            }
            meta_path = session_dir / "meta.json"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            messages_path = session_dir / "messages.ndjson"
            with messages_path.open("w", encoding="utf-8") as handle:
                for row in messages:
                    payload = {
                        "message_id": row["message_id"],
                        "seq": row["seq"],
                        "ts": row["ts"],
                        "intent": row["intent"],
                        "outcome": row["outcome"],
                        "actions": [x for x in (row["actions"] or "").split("|") if x],
                        "learned": [x for x in (row["learned"] or "").split("|") if x],
                        "meta": json.loads(row["meta_json"]) if row["meta_json"] else None,
                        "body": row["body"] or "",
                    }
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            for path in (meta_path, messages_path):
                entries.append({
                    "path": path.relative_to(work).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                })
        archive_id = compute_archive_id(entries)
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "archive_id": archive_id,
            "created_at": created_at,
            "project": {
                "project_id": project_id,
                "display_name": (
                    project_row["display_name"] if project_row is not None else project_id
                ),
                "source_kind": sessions[0]["source_kind"],
            },
            "counts": {"sessions": len(sessions), "messages": total_messages},
            "producer": {"name": "sessionpack", "version": __version__},
            "files": sorted(entries, key=lambda item: item["path"]),
        }
        (work / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temp_zip = out_dir / f".tmp-{archive_id[:12]}.zip"
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
            for entry in manifest["files"]:
                bundle.write(work / entry["path"], entry["path"])
            bundle.write(work / MANIFEST_NAME, MANIFEST_NAME)
        check = verify_archive(temp_zip)
        if not check.ok:
            raise ArchiveError(check.issues)
        final = out_dir / f"{_slug(project_id)}-{created_at[:10]}-{archive_id[:12]}.zip"
        os.replace(temp_zip, final)
        return ArchiveResult(
            path=final,
            archive_id=archive_id,
            project_id=project_id,
            sessions=len(sessions),
            messages=total_messages,
            bytes=final.stat().st_size,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify_archive(path: Path) -> VerifyResult:
    path = Path(path)
    try:
        bundle = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return VerifyResult(path, False,
                            (VerifyIssue("E_ZIP_UNREADABLE", str(exc)[:120]),), None)
    issues: list[VerifyIssue] = []
    with bundle:
        names = bundle.namelist()
        if MANIFEST_NAME not in names:
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_MISSING", "缺少 manifest.json"),),
                                None)
        try:
            manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_INVALID", str(exc)[:120]),), None)
        if manifest.get("format") != FORMAT_NAME:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "format 不匹配"))
        if manifest.get("format_version") != FORMAT_VERSION:
            issues.append(VerifyIssue("E_FORMAT_VERSION", "format_version 不支持"))
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "files 为空"))
            files = []
        seen: set[str] = set()
        for entry in files:
            name = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(name, str) or _unsafe_member(name):
                issues.append(VerifyIssue("E_PATH_ILLEGAL", repr(name)))
                continue
            if name in seen:
                issues.append(VerifyIssue("E_DUPLICATE_PATH", name))
                continue
            seen.add(name)
            if name not in names:
                issues.append(VerifyIssue("E_FILE_MISSING", name))
                continue
            with bundle.open(name) as handle:
                digest = _sha256_stream(handle)
            if digest != entry.get("sha256"):
                issues.append(VerifyIssue("E_HASH_MISMATCH", name))
        for name in sorted(set(names) - seen - {MANIFEST_NAME}):
            issues.append(VerifyIssue("E_UNREGISTERED_FILE", name))
        if not issues and manifest.get("archive_id") != compute_archive_id(files):
            issues.append(VerifyIssue("E_ARCHIVE_ID_MISMATCH", "archive_id 不可复现"))
    return VerifyResult(path, not issues, tuple(issues), manifest)


def import_archive(conn: sqlite3.Connection, path: Path) -> ArchiveImportResult:
    path = Path(path)
    result = verify_archive(path)
    if not result.ok or result.manifest is None:
        raise ArchiveError(result.issues)
    manifest = result.manifest
    project = manifest["project"]
    catalog.upsert_project(conn, ProjectRecord(
        project_id=project["project_id"],
        display_name=project.get("display_name") or project["project_id"],
        local_path_hint=None,
        source_kind=project.get("source_kind") or "archive",
    ))
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO import_runs(run_id, adapter_id, adapter_version, started_at, result)"
        " VALUES(?,?,?,?,?)",
        (run_id, f"archive:{manifest['archive_id']}", manifest["format_version"],
         datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "running"),
    )
    sessions = messages = created = inserted = 0
    with zipfile.ZipFile(path) as bundle:
        for entry in manifest["files"]:
            name = entry["path"]
            if not name.endswith("/meta.json"):
                continue
            session_dir = name.rsplit("/", 1)[0]
            meta = json.loads(bundle.read(name).decode("utf-8"))
            session_id = meta["session_id"]
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
            rows = bundle.read(f"{session_dir}/messages.ndjson").decode("utf-8")
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
    conn.execute(
        "UPDATE import_runs SET finished_at=?, result=?, scanned_files=?,"
        " imported_rows=?, failed_rows=? WHERE run_id=?",
        (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "ok",
         len(manifest["files"]), inserted, 0, run_id),
    )
    conn.commit()
    return ArchiveImportResult(
        archive_id=manifest["archive_id"],
        sessions=sessions,
        messages=messages,
        sessions_created=created,
        messages_inserted=inserted,
    )
