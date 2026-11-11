"""归档格式常量、id 计算与归档构建。包内最底层，不在顶层导入兄弟模块。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sessionpack import __version__

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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(text: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in text)
    cleaned = cleaned.strip("-")[:60]
    return cleaned or "project"


def canonical_zip_name(project_id: str, created_at: str, archive_id: str) -> str:
    """归档包名唯一公式：`<slug>-<YYYY-MM-DD>-<archive_id 前 12 位>.zip`。

    构建侧与同步侧（pull 解信封后重建包名）共用，两处不得各写一份。
    """
    return f"{_slug(project_id)}-{created_at[:10]}-{archive_id[:12]}.zip"


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
    images_dir: Path | None = None,
) -> ArchiveResult:
    from sessionpack.archive.verify import ArchiveError, verify_archive

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
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            messages_path = session_dir / "messages.ndjson"
            with messages_path.open("w", encoding="utf-8", newline="\n") as handle:
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
            # 分享图片随包：meta_json.images 引用 <session_id>/<file>，
            # 字节在 <images_dir>/<session_id>/<file>，打包进 sessions/<id>/images/
            if images_dir is not None:
                for row in messages:
                    try:
                        msg_meta = json.loads(row["meta_json"] or "{}")
                    except ValueError:
                        continue
                    refs = msg_meta.get("images") if isinstance(msg_meta, dict) else None
                    if not isinstance(refs, list):
                        continue
                    for ref in refs:
                        if not isinstance(ref, str) or "/" not in ref:
                            continue
                        src = images_dir / ref
                        if not src.is_file():
                            continue
                        dest = session_dir / "images" / ref.rsplit("/", 1)[-1]
                        if dest.exists():
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dest)
                        entries.append({
                            "path": dest.relative_to(work).as_posix(),
                            "sha256": _sha256_file(dest),
                            "size": dest.stat().st_size,
                        })
        archive_id = compute_archive_id(entries)
        files: list[dict] = sorted(entries, key=lambda item: item["path"])
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
            "files": files,
        }
        (work / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        temp_zip = out_dir / f".tmp-{archive_id[:12]}.zip"
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as bundle:
            for entry in files:
                bundle.write(work / entry["path"], entry["path"])
            bundle.write(work / MANIFEST_NAME, MANIFEST_NAME)
        check = verify_archive(temp_zip)
        if not check.ok:
            temp_zip.unlink(missing_ok=True)
            raise ArchiveError(check.issues)
        final = out_dir / canonical_zip_name(project_id, created_at, archive_id)
        try:
            os.replace(temp_zip, final)
        except OSError:
            temp_zip.unlink(missing_ok=True)
            raise
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
