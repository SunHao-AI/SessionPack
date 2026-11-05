from sessionpack.adapters.trae_memory import TraeMemoryAdapter

__all__ = ["TraeMemoryAdapter", "get_adapter"]


def get_adapter() -> TraeMemoryAdapter:
    return TraeMemoryAdapter()
