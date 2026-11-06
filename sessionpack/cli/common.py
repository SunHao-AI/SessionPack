"""cli 各命令共用的退出码常量与参数助手。"""

from __future__ import annotations

from pathlib import Path

from sessionpack import catalog, config
from sessionpack.paths import (
    Layout,
    default_memory_root,
    ensure_layout,
    resolve_data_dir,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3


def layout(args) -> Layout:
    return ensure_layout(resolve_data_dir(args.data_dir))


def memory_root(args) -> Path:
    return Path(args.memory_root) if args.memory_root else default_memory_root()


def require_catalog(path: Path) -> bool:
    """索引是否就绪（文件存在且已建表）；真现在 catalog.require_catalog。"""
    return catalog.require_catalog(path)


def resolve_sync_dir(args, lay: Layout) -> Path | None:
    if args.dir:
        # 与 paths.resolve_data_dir 对齐：展开并解析为绝对路径后再落配置
        return Path(args.dir).expanduser().resolve()
    return config.sync_dir(lay.root)
