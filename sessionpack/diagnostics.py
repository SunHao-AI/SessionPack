from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from sessionpack import catalog, config
from sessionpack.adapters import get_adapter
from sessionpack.catalog.conn import schema_version
from sessionpack.catalog.read import reconcile_fts
from sessionpack.paths import Layout


def private_db_path() -> Path:
    base = os.environ.get("APPDATA")
    roaming = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return roaming / "Trae CN" / "ModularData" / "ai-agent" / "database.db"


def _sync_section(root: Path) -> dict:
    _, warnings = config.load_config(root)
    target = config.sync_dir(root)
    if target is None:
        return {"configured": False, "dir": None, "exists": False,
                "archives": 0, "bytes": 0, "warnings": warnings}
    exists = target.is_dir()
    files = [p for p in target.glob("*.zip") if p.is_file()] if exists else []
    return {"configured": True, "dir": str(target), "exists": exists,
            "archives": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "warnings": warnings}


def collect(memory_root: Path, layout: Layout) -> dict:
    adapter = get_adapter()
    exists = Path(memory_root).is_dir()
    info = adapter.inspect(memory_root) if exists else None
    database = private_db_path()
    result: dict = {
        "memory_root": str(memory_root),
        "memory_root_exists": exists,
        "adapter": {
            "id": adapter.id,
            "version": adapter.version,
            "source_kind": adapter.source_kind,
            "detected": adapter.detect(memory_root) if exists else False,
        },
        "source": info.as_dict() if info else None,
        "private_db": {
            "path": str(database),
            "exists": database.is_file(),
            "size": database.stat().st_size if database.is_file() else 0,
            "readable": False,
            "note": "加密私有库，不在读取白名单内，仅报告存在性与体积",
        },
        "catalog": {
            "path": str(layout.catalog),
            "exists": layout.catalog.is_file(),
            "ready": False,
            "stats": {"projects": 0, "sessions": 0, "messages": 0, "fts_rows": 0},
            "tokenizer": "none",
            "version": 0,
            "fts": {"fts_missing": 0, "fts_extra": 0, "drift": False},
            "size_bytes": 0,
            "archives_bytes": 0,
        },
        "sync": _sync_section(layout.root),
        "warnings": [] if exists else ["记忆目录不存在"],
    }
    if layout.catalog.is_file():
        result["catalog"]["size_bytes"] = layout.catalog.stat().st_size
    archives = layout.archives
    if archives.is_dir():
        result["catalog"]["archives_bytes"] = sum(
            p.stat().st_size for p in archives.rglob("*") if p.is_file()
        )
    if layout.catalog.is_file():
        try:
            conn = catalog.connect_readonly(layout.catalog)
        except sqlite3.Error:
            result["warnings"].append("索引库无法打开，请重新运行 import")
            return result
        try:
            try:
                ready = catalog.has_schema(conn)
            except sqlite3.DatabaseError:
                ready = False
            if ready:
                result["catalog"]["ready"] = True
                result["catalog"]["stats"] = catalog.stats(conn)
                result["catalog"]["tokenizer"] = catalog.tokenizer_of(conn)
                result["catalog"]["version"] = schema_version(conn)
                fts = reconcile_fts(conn)
                result["catalog"]["fts"] = {
                    "fts_missing": fts["fts_missing"],
                    "fts_extra": fts["fts_extra"],
                    "drift": fts["drift"],
                }
                if fts["drift"]:
                    miss, extra = fts["fts_missing"], fts["fts_extra"]
                    result["warnings"].append(
                        f"FTS 与 messages 漂移（缺失 {miss}、多余 {extra}），"
                        "运行 sessionpack rebuild --fts 修复")
            else:
                result["warnings"].append("索引未初始化，请重新运行 import")
        finally:
            conn.close()
    return result
