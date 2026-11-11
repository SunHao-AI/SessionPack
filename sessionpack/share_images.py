"""分享图片的字节落位与路径解析。

统一口径：图片字节存 ``<data-dir>/images/<session_id>/<file>``，消息
meta_json 的 ``images`` 存 ``<session_id>/<file>`` 相对引用。本机抓取、
归档构建、他机安装、Web 服务四处共用本模块。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from sessionpack.catalog.conn import SESSION_ID_RE

_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


def link_share_images(catalog_db: Path, export_root: Path) -> int:
    """把导出目录里的分享图片复制到数据目录 images/，返回复制（含跳过）数。

    通过 meta_json 中的 share 专属键 ``slug`` 检索（其他适配器不产生）。
    目标已存在且大小相同则跳过（幂等）。
    """
    conn = sqlite3.connect(catalog_db)
    try:
        rows = conn.execute(
            "SELECT session_id, meta_json FROM messages "
            "WHERE meta_json LIKE '%\"slug\":%'").fetchall()
    finally:
        conn.close()

    count = 0
    images_root = Path(catalog_db).parent / "images"
    for session_id, meta_raw in rows:
        try:
            meta = json.loads(meta_raw or "{}")
        except ValueError:
            continue
        refs = meta.get("images")
        slug = meta.get("slug")
        if not isinstance(refs, list) or not isinstance(slug, str) or not slug:
            continue
        for ref in refs:
            if not isinstance(ref, str):
                continue
            fname = ref.rsplit("/", 1)[-1]
            src = export_root / "images" / slug / fname
            if not src.is_file():
                continue
            dest = images_root / session_id / fname
            if dest.is_file() and dest.stat().st_size == src.stat().st_size:
                count += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            count += 1
    return count


def resolve_image(images_root: Path, rel: str) -> Path | None:
    """把 ``<session_id>/<file>`` 相对引用解析为 images_root 内的真实路径。

    任何越界（非法 session_id、路径穿越、符号链接逃逸）返回 None。
    """
    parts = rel.split("/")
    if len(parts) != 2 or not SESSION_ID_RE.fullmatch(parts[0]):
        return None
    fname = parts[1]
    if ".." in fname or not _FILENAME_RE.fullmatch(fname):
        return None
    root = images_root.resolve()
    target = (root / parts[0] / fname).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target
