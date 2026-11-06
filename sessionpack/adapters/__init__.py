from sessionpack.adapters.base import MemoryAdapter
from sessionpack.adapters.trae_memory import TraeMemoryAdapter

__all__ = ["MemoryAdapter", "TraeMemoryAdapter", "get_adapter"]


def get_adapter() -> MemoryAdapter:
    return TraeMemoryAdapter()
