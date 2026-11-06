from __future__ import annotations

import json
import os
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


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _dump_toml(data: dict) -> str:
    """序列化已知形态：顶层标量 + 扁平值段（tomllib 只读，需手写输出）。"""
    scalars = []
    sections = []
    for key, value in data.items():
        if isinstance(value, dict):
            body = "".join(f"{k} = {_toml_value(v)}\n" for k, v in value.items())
            sections.append(f"[{key}]\n{body}")
        else:
            scalars.append(f"{key} = {_toml_value(value)}\n")
    return "".join(scalars) + "".join(sections)


def set_sync_dir(root: Path, directory: Path) -> None:
    """合并式写回：保留既有其他配置段，原子替换（临时文件 + os.replace）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data, _ = load_config(root)
    data = dict(data)
    section = data.get("sync")
    section = dict(section) if isinstance(section, dict) else {}
    section["dir"] = str(Path(directory))
    data["sync"] = section
    path = root / CONFIG_NAME
    tmp = root / (CONFIG_NAME + ".tmp")
    tmp.write_text(_dump_toml(data), encoding="utf-8")
    os.replace(tmp, path)
