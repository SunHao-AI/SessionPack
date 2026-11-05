from __future__ import annotations

import os
from pathlib import Path

from sessionpack import catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.paths import Layout


def private_db_path() -> Path:
    base = os.environ.get("APPDATA")
    roaming = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return roaming / "Trae CN" / "ModularData" / "ai-agent" / "database.db"


def collect(memory_root: Path, layout: Layout) -> dict:
    adapter = TraeMemoryAdapter()
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
            "stats": {"projects": 0, "sessions": 0, "messages": 0, "fts_rows": 0},
            "tokenizer": "none",
        },
        "warnings": [] if exists else ["记忆目录不存在"],
    }
    if layout.catalog.is_file():
        conn = catalog.connect(layout.catalog)
        try:
            result["catalog"]["stats"] = catalog.stats(conn)
            result["catalog"]["tokenizer"] = catalog.tokenizer_of(conn)
        finally:
            conn.close()
    return result
