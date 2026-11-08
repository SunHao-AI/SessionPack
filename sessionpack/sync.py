from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from sessionpack import archive
from sessionpack.textutil import failure_text

_TAIL_RE = re.compile(r"-([0-9a-f]{12})\.zip$")
_TEMP_PREFIXES = (".tmp-", ".build-")


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


def _scan_zips(directory: Path) -> list[Path]:
    """目录内的归档包候选；跳过原子写强杀残留的 .tmp-/.build- 前缀文件。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.zip")
                  if not p.name.startswith(_TEMP_PREFIXES))


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 临时名带写者标识：并发写者不再互踩在途临时文件（WinError 32）。
    # 保留 .tmp- 前缀且不以 .zip 结尾，_scan_zips 的残留跳过语义不变。
    tmp = dest.parent / (
        f".tmp-{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except (PermissionError, FileExistsError):
        # 目标正被他进程持有：同名包内容必同（文件名尾 12 位是内容
        # sha256），视为"已由他人落定"良性消化；否则原样上抛。
        if not dest.exists():
            raise
    finally:
        tmp.unlink(missing_ok=True)


def push_archives(archives_dir: Path, sync_dir: Path,
                  dry_run: bool = False) -> SyncResult:
    pushed: list[str] = []
    existing = 0
    unverified: list[str] = []
    errors: list[str] = []
    total = 0
    local = _scan_zips(archives_dir)
    remote_names = {p.name for p in _scan_zips(sync_dir)}
    for src in local:
        if src.name in remote_names:
            existing += 1
            continue
        check = archive.verify_archive(src)
        if not check.ok:
            codes = ", ".join(issue.code for issue in check.issues)
            unverified.append(f"{src.name}: {codes}")
            continue
        try:
            size = src.stat().st_size
        except OSError as exc:
            errors.append(f"{src.name}: {failure_text(exc)}")
            continue
        if not dry_run:
            try:
                _atomic_copy(src, Path(sync_dir) / src.name)
            except OSError as exc:
                errors.append(f"{src.name}: {failure_text(exc)}")
                continue
        pushed.append(src.name)
        total += size
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
    local_ids = {tail for p in _scan_zips(archives_dir)
                 if (tail := _id_tail(p.name))}
    # 无 id12 尾巴的包以文件名判同，避免每轮重复复制+导入；
    # .tmp-/.build- 前缀的强杀残留不算"已存在"，可被规范包覆盖清掉
    local_names = {p.name for p in _scan_zips(archives_dir)}
    for src in _scan_zips(sync_dir):
        tail = _id_tail(src.name)
        if (tail and tail in local_ids) or src.name in local_names:
            existing += 1
            continue
        check = archive.verify_archive(src)
        if not check.ok:
            codes = ", ".join(issue.code for issue in check.issues)
            invalid.append(f"{src.name}: {codes}")
            continue
        dest = archives_dir / src.name
        try:
            size = src.stat().st_size
            _atomic_copy(src, dest)
        except OSError as exc:
            errors.append(f"{src.name}: {failure_text(exc)}")
            continue
        try:
            imported = archive.import_archive(conn, dest)
        except (archive.ArchiveError, OSError, sqlite3.Error) as exc:
            errors.append(f"{src.name}: 已复制但导入失败 "
                          f"{failure_text(exc)}")
            continue
        pulled += 1
        total += size
        sessions_added += imported.sessions_created
        messages_added += imported.messages_inserted
        if tail:
            local_ids.add(tail)
        local_names.add(src.name)
    return SyncResult(pulled=pulled, skipped_existing=existing,
                      skipped_invalid=tuple(invalid),
                      sessions_added=sessions_added,
                      messages_added=messages_added,
                      bytes=total, errors=tuple(errors))
