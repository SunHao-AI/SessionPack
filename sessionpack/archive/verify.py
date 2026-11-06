"""归档包完整性与安全校验。"""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sessionpack.archive.build import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    compute_archive_id,
)
from sessionpack.textutil import failure_text


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


class ArchiveError(RuntimeError):
    def __init__(self, issues: tuple[VerifyIssue, ...] | list[VerifyIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.code}: {i.detail}" for i in self.issues))


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _unsafe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return True
    pure = PurePosixPath(name)
    return pure.is_absolute() or ".." in pure.parts


def _invalid_json_line(bundle: zipfile.ZipFile, name: str) -> int | None:
    with bundle.open(name) as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return number
    return None


def verify_archive(path: Path) -> VerifyResult:
    path = Path(path)
    try:
        bundle = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return VerifyResult(path, False,
                            (VerifyIssue("E_ZIP_UNREADABLE",
                                         failure_text(exc, 120)),), None)
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
                                (VerifyIssue("E_MANIFEST_INVALID",
                                             failure_text(exc, 120)),), None)
        if not isinstance(manifest, dict):
            return VerifyResult(path, False,
                                (VerifyIssue("E_MANIFEST_INVALID", "manifest 顶层必须是对象"),),
                                None)
        if manifest.get("format") != FORMAT_NAME:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "format 不匹配"))
        if manifest.get("format_version") != FORMAT_VERSION:
            issues.append(VerifyIssue("E_FORMAT_VERSION", "format_version 不支持"))
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            issues.append(VerifyIssue("E_MANIFEST_INVALID", "files 为空"))
            files = []
        seen: set[str] = set()
        verified: set[str] = set()
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
            else:
                verified.add(name)
        for name in sorted(set(names) - seen - {MANIFEST_NAME}):
            issues.append(VerifyIssue("E_UNREGISTERED_FILE", name))
        for name in sorted(seen):
            if not name.endswith("/meta.json"):
                continue
            sibling = name[: -len("meta.json")] + "messages.ndjson"
            if sibling not in seen:
                issues.append(VerifyIssue("E_SESSION_INCOMPLETE", name))
                continue
            if sibling in verified:
                bad_line = _invalid_json_line(bundle, sibling)
                if bad_line is not None:
                    issues.append(VerifyIssue("E_NDJSON_INVALID", f"{sibling}:{bad_line}"))
        if not issues and manifest.get("archive_id") != compute_archive_id(files):
            issues.append(VerifyIssue("E_ARCHIVE_ID_MISMATCH", "archive_id 不可复现"))
    return VerifyResult(path, not issues, tuple(issues), manifest)
