from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sessionpack import archive, crypto
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


def _is_envelope_name(name: str) -> bool:
    return name.startswith(crypto.NAME_PREFIX) and name.endswith(crypto.NAME_SUFFIX)


def _scan_sync_candidates(directory: Path) -> list[Path]:
    """同步目录候选：`*.zip` + `*.spk`，各自排序；隐藏名与临时前缀不入候选。

    本机 archives/ 不用本函数（仍由 _scan_zips 只认 zip，spec §4 D7）。
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out: list[Path] = []
    for pattern in ("*.zip", f"*{crypto.NAME_SUFFIX}"):
        for p in sorted(directory.glob(pattern)):
            if p.name.startswith(".") or p.name.startswith(_TEMP_PREFIXES):
                continue
            out.append(p)
    return out


def _envelope_archive_id(path: Path, passphrase: str) -> str:
    """解出信封内 zip 的 archive_id；全程只在内存，不落盘。"""
    data = crypto.decrypt_envelope(path.read_bytes(), passphrase, path.name)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read(archive.MANIFEST_NAME))
    return manifest["archive_id"]


def _atomic_write(tmp: Path, blob: bytes, dest: Path) -> None:
    """字节级原子落盘：确保父目录存在，写临时名再 os.replace；
    目标已存在视为并发先到，良性跳过（与 _atomic_copy 同口径）。"""
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
    except (PermissionError, FileExistsError) as exc:
        tmp.unlink(missing_ok=True)
        if dest.exists():
            return
        raise exc
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def find_archives_with_session(directory: Path, session_id: str) -> list[str]:
    """只读探测：directory 下含该会话**内容**的包文件名（Spec 13 D6 报告用）。

    已替换为 purge 卡的包（meta status='purged'、正文为空）不算命中——本机
    重打包循环与同步目录报告都只关心"还带着内容"的包。读不了的包按"不含"
    跳过：报告场景宁漏勿错，不阻塞命令。
    """
    if not directory.is_dir():
        return []
    card = f"sessions/{session_id}/meta.json"
    hits: list[str] = []
    for path in _scan_zips(directory):
        try:
            with zipfile.ZipFile(path) as zf:
                if card not in zf.namelist():
                    continue
                if json.loads(zf.read(card)).get("status") == "purged":
                    continue
                hits.append(path.name)
        except (OSError, zipfile.BadZipFile, KeyError, ValueError,
                AttributeError):
            continue
    return hits


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


def push_archives(archives_dir: Path, sync_dir: Path, dry_run: bool = False,
                  *, encrypt: bool = False,
                  passphrase: str | None = None) -> SyncResult:
    """把本地包推进同步目录（只增不删，G7：源目录零改动）。

    encrypt=True 时把每个包封成随机名 .spk 信封落盘，判重靠解出信封内
    zip 的 archive_id（信封名与内容无关，无法按文件名比对）；
    encrypt=False 时行为与历史版本逐字节一致。
    """
    if encrypt and not passphrase:
        raise ValueError("E201: 加密推送需要口令")
    pushed: list[str] = []
    existing = 0
    unverified: list[str] = []
    errors: list[str] = []
    total = 0
    candidates = _scan_sync_candidates(sync_dir)
    remote_names = {p.name for p in candidates}
    have_ids: set[str] = set()
    if encrypt:
        assert passphrase is not None      # 收窄：入口已校验 E201
        for p in candidates:
            if not _is_envelope_name(p.name):
                continue
            try:
                have_ids.add(_envelope_archive_id(p, passphrase))
            except (crypto.EnvelopeError, OSError, ValueError, KeyError,
                    zipfile.BadZipFile):
                continue          # 无关或损坏信封不参与判重，也不被本命令改动
    for src in _scan_zips(archives_dir):
        if encrypt:
            assert passphrase is not None      # 收窄：入口已校验 E201
            check = archive.verify_archive(src)
            if not check.ok or check.manifest is None:
                codes = ", ".join(issue.code for issue in check.issues)
                unverified.append(f"{src.name}: {codes or '无 manifest'}")
                continue
            if check.manifest["archive_id"] in have_ids:
                existing += 1
                continue
            try:
                size = src.stat().st_size
            except OSError as exc:
                errors.append(f"{src.name}: {failure_text(exc)}")
                continue
            if not dry_run:
                name = crypto.new_envelope_name()
                try:
                    blob = crypto.encrypt_envelope(src.read_bytes(),
                                                   passphrase, name)
                    tmp = sync_dir / f".tmp-{name}." \
                                     f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
                    _atomic_write(tmp, blob, sync_dir / name)
                except (OSError, crypto.EnvelopeError) as exc:
                    errors.append(f"{src.name}: {failure_text(exc)}")
                    continue
            pushed.append(src.name)
            total += size
            continue
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
