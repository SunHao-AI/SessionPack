from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import archive

_TAIL_RE = re.compile(r"-([0-9a-f]{12})\.zip$")


@dataclass
class SyncResult:
    pushed: int = 0
    pulled: int = 0
    skipped_existing: int = 0
    skipped_unverified: tuple[str, ...] = ()
    skipped_invalid: tuple[str, ...] = ()
    sessions_added: int = 0
    messages_added: int = 0
    bytes: int = 0
    errors: tuple[str, ...] = ()


def _id_tail(name: str) -> str | None:
    match = _TAIL_RE.search(name)
    return match.group(1) if match else None


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (".tmp-" + dest.name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def push_archives(archives_dir: Path, sync_dir: Path,
                  dry_run: bool = False) -> SyncResult:
    pushed: list[str] = []
    existing = 0
    unverified: list[str] = []
    errors: list[str] = []
    total = 0
    local = sorted(Path(archives_dir).glob("*.zip"))
    remote_names = (
        {p.name for p in Path(sync_dir).glob("*.zip")}
        if Path(sync_dir).is_dir() else set()
    )
    for src in local:
        if src.name in remote_names:
            existing += 1
            continue
        check = archive.verify_archive(src)
        if not check.ok:
            codes = ", ".join(issue.code for issue in check.issues)
            unverified.append(f"{src.name}: {codes}")
            continue
        if not dry_run:
            try:
                _atomic_copy(src, Path(sync_dir) / src.name)
            except OSError as exc:
                errors.append(f"{src.name}: {archive._failure_text(exc)}")
                continue
        pushed.append(src.name)
        total += src.stat().st_size
    return SyncResult(pushed=len(pushed), skipped_existing=existing,
                      skipped_unverified=tuple(unverified),
                      bytes=total, errors=tuple(errors))


def pull_archives(archives_dir: Path, sync_dir: Path, conn) -> SyncResult:
    archives_dir = Path(archives_dir)
    pulled = 0
    existing = 0
    invalid: list[str] = []
    errors: list[str] = []
    sessions_added = 0
    messages_added = 0
    total = 0
    local_ids = {tail for p in archives_dir.glob("*.zip")
                 if (tail := _id_tail(p.name))}
    for src in sorted(Path(sync_dir).glob("*.zip")):
        tail = _id_tail(src.name)
        if tail and tail in local_ids:
            existing += 1
            continue
        check = archive.verify_archive(src)
        if not check.ok:
            codes = ", ".join(issue.code for issue in check.issues)
            invalid.append(f"{src.name}: {codes}")
            continue
        size = src.stat().st_size
        dest = archives_dir / src.name
        try:
            _atomic_copy(src, dest)
        except OSError as exc:
            errors.append(f"{src.name}: {archive._failure_text(exc)}")
            continue
        try:
            imported = archive.import_archive(conn, dest)
        except (archive.ArchiveError, OSError) as exc:
            errors.append(f"{src.name}: 已复制但导入失败 "
                          f"{archive._failure_text(exc)}")
            continue
        pulled += 1
        total += size
        sessions_added += imported.sessions_created
        messages_added += imported.messages_inserted
        if tail:
            local_ids.add(tail)
    return SyncResult(pulled=pulled, skipped_existing=existing,
                      skipped_invalid=tuple(invalid),
                      sessions_added=sessions_added,
                      messages_added=messages_added,
                      bytes=total, errors=tuple(errors))
