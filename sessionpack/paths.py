from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SessionPack"


@dataclass(frozen=True)
class Layout:
    root: Path
    catalog: Path
    archives: Path


def default_data_dir() -> Path:
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or str(Path.home() / ".local" / "share")
    )
    return Path(base) / APP_NAME


def resolve_data_dir(value: str | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_data_dir()


def ensure_layout(root: Path) -> Layout:
    layout = Layout(
        root=root,
        catalog=root / "catalog.sqlite",
        archives=root / "archives",
    )
    for directory in (root, layout.archives):
        directory.mkdir(parents=True, exist_ok=True)
    return layout


def default_memory_root() -> Path:
    return Path.home() / ".trae-cn" / "memory"
