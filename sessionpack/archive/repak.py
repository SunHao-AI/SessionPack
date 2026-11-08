"""purge 后对既有归档包做原位剔除（Spec 13 D5）。

纯文件操作：不碰数据库、零 SQL 字面量。先 verify，再逐条复制原包条目——只把
目标会话的 meta 换成 purged 精简卡、ndjson 换成 0 字节空文件（verify 的
meta↔ndjson 配对不变量不允许只删一条），其余条目字节级原样——manifest 重算
files/counts/archive_id 后临时落地、自检、原子改名。旧包删除由调用方负责
（本函数失败时旧包原样在场，可回退）。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

# 同包互导私有工具函数属 lint_imports 豁免（E_PRIVATE 同包豁免）
from sessionpack.archive.build import MANIFEST_NAME, _sha256_bytes, _slug, compute_archive_id
from sessionpack.archive.verify import ArchiveError, verify_archive


class RepakError(Exception):
    """重打包前置条件不满足（包内没有目标会话等）。"""


def _purged_card(old_meta: dict, session_id: str) -> dict:
    """purged 精简卡：只留身份与时间字段，不含 title/summary 等内容字段。"""
    project_id = old_meta.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RepakError(f"会话 {session_id} 的 meta 缺少 project_id，无法出卡")
    return {
        "session_id": session_id,
        "project_id": project_id,
        "source_kind": old_meta.get("source_kind") or "session",
        "status": "purged",
        "created_at": old_meta.get("created_at") or "",
        "updated_at": (old_meta.get("updated_at")
                       or old_meta.get("created_at") or ""),
    }


def repak_excluding_session(src: Path, session_id: str, dest_dir: Path) -> Path:
    """把 src 包中 session_id 替换为 purge 卡后写入 dest_dir，返回新包路径。"""
    check = verify_archive(src)
    if not check.ok:
        raise ArchiveError(check.issues)
    card_name = f"sessions/{session_id}/meta.json"
    ndjson_name = f"sessions/{session_id}/messages.ndjson"
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(src) as zf:
        manifest = json.loads(zf.read(MANIFEST_NAME))
        if card_name not in zf.namelist():
            raise RepakError(f"包内无会话 {session_id}")
        # 手造畸形包可能缺 project 节：提前取值出 RepakError，不让 KeyError
        # 穿透到调用方（write_cmds 的捕获元组里没有 KeyError，会中断整个 repak 循环）
        project_meta = manifest.get("project")
        project_id = (project_meta.get("project_id")
                      if isinstance(project_meta, dict) else None)
        if not isinstance(project_id, str) or not project_id:
            raise RepakError("包 manifest 缺少 project.project_id，无法命名新包")
        card = _purged_card(json.loads(zf.read(card_name)), session_id)
        card_bytes = (json.dumps(card, ensure_ascii=False, indent=2)
                      + "\n").encode("utf-8")
        for info in zf.infolist():
            if info.filename == MANIFEST_NAME:
                continue                      # manifest 统一重建
            if info.filename == card_name:
                entries.append((card_name, card_bytes))
            elif info.filename == ndjson_name:
                entries.append((ndjson_name, b""))   # 空 ndjson 合法
            else:
                entries.append((info.filename, zf.read(info.filename)))
    files = [{"path": p, "sha256": _sha256_bytes(b), "size": len(b)}
             for p, b in entries]
    sessions = sum(1 for p, _ in entries
                   if p.startswith("sessions/") and p.endswith("/meta.json"))
    messages = sum(1 for p, b in entries if p.endswith("messages.ndjson")
                   for line in b.splitlines() if line.strip())
    archive_id = compute_archive_id(files)
    manifest["archive_id"] = archive_id
    manifest["files"] = files
    manifest["counts"] = {"sessions": sessions, "messages": messages}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2)
                      + "\n").encode("utf-8")
    dest_dir.mkdir(parents=True, exist_ok=True)
    temp = dest_dir / f".tmp-repak-{archive_id[:12]}.zip"
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        zf.writestr(MANIFEST_NAME, manifest_bytes)
    recheck = verify_archive(temp)
    if not recheck.ok:
        temp.unlink(missing_ok=True)
        raise ArchiveError(recheck.issues)
    # 日期段沿用原包 created_at：本机改名只由 archive_id 段体现
    # project_id 已在函数开头校验取值，此处直接复用
    date_seg = (manifest.get("created_at") or "")[:10]
    final = dest_dir / (f"{_slug(project_id)}"
                        f"-{date_seg}-{archive_id[:12]}.zip")
    os.replace(temp, final)
    return final
