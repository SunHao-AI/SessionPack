from __future__ import annotations

import json
import tomllib
from pathlib import Path

CONFIG_NAME = "config.toml"


def load_config(root: Path) -> tuple[dict, list[str]]:
    """读取 <root>/config.toml；缺失或损坏按空配置处理并返回警告。"""
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        return {}, []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}, ["config.toml 解析失败，已按空配置处理"]
    return (data, []) if isinstance(data, dict) else ({}, [])


def sync_dir(root: Path) -> Path | None:
    data, _ = load_config(root)
    section = data.get("sync")
    if not isinstance(section, dict):
        return None
    raw = section.get("dir")
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw).expanduser()


def set_sync_dir(root: Path, directory: Path) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    text = "[sync]\ndir = " + json.dumps(str(Path(directory))) + "\n"
    (root / CONFIG_NAME).write_text(text, encoding="utf-8")
