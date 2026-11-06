"""diagnostics 只经 get_adapter 工厂取适配器。"""
from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from sessionpack.adapters import MemoryAdapter, get_adapter
from sessionpack.adapters.trae_memory import TraeMemoryAdapter

# MemoryAdapter 是非 runtime_checkable 的 Protocol，isinstance 不可用，
# 故逐槽位核验结构契约（静态面由 get_adapter 返回注解经 mypy 把关）。
PROTOCOL_SLOTS = ("id", "version", "source_kind", "detect", "inspect",
                  "declared_paths", "enumerate", "read_file")


def test_get_adapter_returns_protocol_instance() -> None:
    assert get_type_hints(get_adapter)["return"] is MemoryAdapter
    adapter = get_adapter()
    assert isinstance(adapter, TraeMemoryAdapter)
    assert [s for s in PROTOCOL_SLOTS if not hasattr(adapter, s)] == []


def test_diagnostics_source_does_not_import_concrete_adapter() -> None:
    src = (Path(__file__).resolve().parents[1]
           / "sessionpack" / "diagnostics.py").read_text(encoding="utf-8")
    assert "TraeMemoryAdapter" not in src
    assert "get_adapter" in src
