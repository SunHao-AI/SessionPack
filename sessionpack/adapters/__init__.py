from sessionpack.adapters.base import MemoryAdapter
from sessionpack.adapters.trae_export import TraeExportAdapter
from sessionpack.adapters.trae_memory import TraeMemoryAdapter

__all__ = ["MemoryAdapter", "TraeMemoryAdapter", "TraeExportAdapter",
           "get_adapter", "get_export_adapter"]


def get_adapter() -> MemoryAdapter:
    return TraeMemoryAdapter()


def get_export_adapter() -> MemoryAdapter:
    return TraeExportAdapter()
