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
    # push 加密判重阶段无法解密的同步目录信封数（B1：透明化，不改重推行为）
    skipped_undecryptable: int = 0
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
    return crypto.is_envelope_name(name)


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


def find_archives_with_session(directory: Path, session_id: str,
                               passphrase: str | None = None) -> list[str]:
    """列出 directory 中含该会话（未 purge 卡）的包文件名。

    明文 zip 逐字节按现状扫；passphrase 非 None 时另解 `*.spk` 信封（D9）。
    解密失败按"不含"跳过——维持 purge 报告"宁漏勿错"取态：报告漏报优于
    把口令错误的无关信封诬报为"仍含旧包"。
    """
    if not directory.is_dir():
        return []
    card = f"sessions/{session_id}/meta.json"
    hits: list[str] = []
    for path in _scan_sync_candidates(directory):
        if _is_envelope_name(path.name):
            if passphrase is None:
                continue
            try:
                data = crypto.decrypt_envelope(path.read_bytes(), passphrase,
                                               path.name)
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    if card not in zf.namelist():
                        continue
                    if json.loads(zf.read(card)).get("status") == "purged":
                        continue
            except (crypto.EnvelopeError, OSError, zipfile.BadZipFile, KeyError,
                    ValueError, AttributeError):
                continue
            hits.append(path.name)
            continue
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
                  passphrase: str | None = None,
                  known_ids: dict[str, str] | None = None) -> SyncResult:
    """把本地包推进同步目录（只增不删，G7：源目录零改动）。

    encrypt=True 时把每个包封成随机名 .spk 信封落盘，判重靠解出信封内
    zip 的 archive_id（信封名与内容无关，无法按文件名比对）；
    encrypt=False 时行为与历史版本逐字节一致。

    known_ids（B2 判重性能）：调用方持有的「信封文件名→archive_id」本机
    清单。archive_id 由内容 sha256 派生、与口令无关，且 push 只新增随机名
    信封、从不就地改写既有信封，故一条 name→archive_id 一旦学到即恒成立，
    命中可直接信任免 PBKDF2。未命中（新名/换机）回退解密并回填；同步目录已
    不存在的信封名清出清单防僵尸项。None 时行为与无缓存完全一致，但仍回填
    本轮解出的 id 供调用方持久化。B1 的无法解密计数语义不变（仅在未命中
    且解密失败时计入）。
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
    undecryptable = 0
    if encrypt:
        assert passphrase is not None      # 收窄：入口已校验 E201
        for p in candidates:
            if not _is_envelope_name(p.name):
                continue
            cached = known_ids.get(p.name) if known_ids is not None else None
            if cached is not None:
                have_ids.add(cached)
                continue
            try:
                aid = _envelope_archive_id(p, passphrase)
            except (crypto.EnvelopeError, OSError, ValueError, KeyError,
                    zipfile.BadZipFile):
                undecryptable += 1  # 无关或损坏信封不参与判重，也不被本命令改动
                continue
            have_ids.add(aid)
            if known_ids is not None:
                known_ids[p.name] = aid
        if known_ids is not None:      # 同步目录已不存在的信封清出清单，防僵尸项增长
            live = {p.name for p in candidates if _is_envelope_name(p.name)}
            for stale in [k for k in known_ids if k not in live]:
                del known_ids[stale]
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
                if known_ids is not None:      # 新推信封回填清单（B2）
                    known_ids[name] = check.manifest["archive_id"]
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
                      skipped_undecryptable=undecryptable,
                      bytes=total, errors=tuple(errors))


def pull_archives(archives_dir: Path, sync_dir: Path, conn,
                  *, encrypt: bool = False,
                  passphrase: str | None = None,
                  known_ids: dict[str, str] | None = None,
                  images_dir: Path | None = None) -> SyncResult:
    """把同步目录的包拉进本机 archives/ 并导入索引（只增不删，G7：源零改动）。

    `*.zip` 分支与历史版本逐字节一致；encrypt=True 时另解 `*.spk` 信封：
    内存解密 → 规范包名原子落盘 → verify → import，解密/结构失败一律入
    skipped_invalid（口令错与文件损坏对用户同为"不可用"，报告不区分 E201/E202，
    也避免异常文本混入本机路径）。

    known_ids（B2 幂等性能）：与 push 共用的「信封文件名→archive_id」本机
    清单。命中且本机已有同 id12 尾巴的包时直接计 skipped_existing、全程零
    解密——与无缓存分支的判重条件（archive_id[:12] in local_ids）等价，
    结果逐字节一致，只是免掉 PBKDF2。未命中（新名/换机）回退解密并回填；
    同步目录已不存在的信封名清出清单防僵尸项。None 时行为与无缓存一致。
    """
    if encrypt and not passphrase:
        raise ValueError("E201: 加密拉取需要口令")
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
    candidates = _scan_sync_candidates(sync_dir)
    if known_ids is not None:      # 与 push 同一份清单：同步目录已不存在的信封清出，防僵尸项
        live = {p.name for p in candidates if _is_envelope_name(p.name)}
        for stale in [k for k in known_ids if k not in live]:
            del known_ids[stale]
    for src in candidates:
        if not _is_envelope_name(src.name):
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
                imported = archive.import_archive(conn, dest,
                                                  images_dir=images_dir)
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
            continue
        if not encrypt:
            continue            # 不开加密时信封视为不可见（零行为变更）
        assert passphrase is not None      # 收窄：入口已校验 E201
        cached_id = known_ids.get(src.name) if known_ids is not None else None
        if cached_id is not None and cached_id[:12] in local_ids:
            # B2：清单命中且本机已有同 id12 尾巴的包才短路（与原信封分支
            # `archive_id[:12] in local_ids` 判据一致）；对不上则落回解密原路径
            existing += 1
            continue
        try:
            blob = src.read_bytes()
        except OSError as exc:
            errors.append(f"{src.name}: {failure_text(exc)}")
            continue
        size = len(blob)          # 记解密前的信封字节数，与 push 侧口径对称
        try:
            data = crypto.decrypt_envelope(blob, passphrase, src.name)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                manifest = json.loads(zf.read(archive.MANIFEST_NAME))
            archive_id = manifest["archive_id"]
            canon = archive.canonical_zip_name(
                manifest["project"]["project_id"], manifest["created_at"],
                archive_id)
        except (crypto.EnvelopeError, zipfile.BadZipFile, KeyError, TypeError,
                ValueError):
            if known_ids is not None:
                known_ids.pop(src.name, None)  # 清单失效（口令轮换/文件损坏）
            invalid.append(f"{src.name}: 口令错误或文件损坏")
            continue
        if known_ids is not None:
            known_ids[src.name] = archive_id
        if archive_id[:12] in local_ids or canon in local_names:
            existing += 1
            continue
        dest = archives_dir / canon
        tmp = archives_dir / f".tmp-{canon}.{os.getpid()}." \
                             f"{uuid.uuid4().hex[:8]}"
        try:
            _atomic_write(tmp, data, dest)
        except OSError as exc:
            errors.append(f"{src.name}: {failure_text(exc)}")
            continue
        check = archive.verify_archive(dest)
        if not check.ok:
            dest.unlink(missing_ok=True)
            invalid.append(f"{src.name}: "
                           f"{', '.join(i.code for i in check.issues)}")
            continue
        try:
            imported = archive.import_archive(conn, dest, images_dir=images_dir)
        except (archive.ArchiveError, OSError, sqlite3.Error) as exc:
            errors.append(f"{src.name}: 已落盘但导入失败 "
                          f"{failure_text(exc)}")
            continue
        pulled += 1
        sessions_added += imported.sessions_created
        messages_added += imported.messages_inserted
        total += size
        local_ids.add(archive_id[:12])
        local_names.add(canon)
    return SyncResult(pulled=pulled, skipped_existing=existing,
                      skipped_invalid=tuple(invalid),
                      sessions_added=sessions_added,
                      messages_added=messages_added,
                      bytes=total, errors=tuple(errors))
