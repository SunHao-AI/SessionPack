from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import date, time
from pathlib import Path

CONFIG_NAME = "config.toml"

# _dump_toml 对键名一律不加引号，裸键名之外的键写出后不可解析
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


class ConfigWriteError(RuntimeError):
    """既有 config.toml 无法被保真写回时拒绝写入（fail-closed）。"""


def _read_toml(root: Path) -> tuple[dict, list[str]]:
    """读取并解析 config.toml；警告只含"致命"类（解析失败/不可读）。"""
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        return {}, []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}, ["config.toml 解析失败，已按空配置处理"]
    return (data, []) if isinstance(data, dict) else ({}, [])


def load_config(root: Path) -> tuple[dict, list[str]]:
    """读取 <root>/config.toml；缺失或损坏按空配置处理并返回警告。"""
    return _read_toml(root)


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


def _unfaithful_value(key: str, value) -> list[str]:
    """段内值/顶层值能否被 _dump_toml 保真往返；返回不可保真原因。"""
    if isinstance(value, dict):
        return [f"{key}（嵌套段）"]
    if isinstance(value, (date, time)):
        return [f"{key}（日期/时间）"]
    if isinstance(value, list):
        bad = [item for item in value
               if not isinstance(item, (bool, int, float, str))]
        if bad:
            return [f"{key}（列表含非标量元素）"]
    return []


def _unfaithful(data: dict) -> list[str]:
    """data 经 _dump_toml 写回后能否保真往返；返回原因列表（空=可写）。"""
    problems: list[str] = []
    for key, value in data.items():
        if not _BARE_KEY_RE.fullmatch(str(key)):
            problems.append(f"{key}（键名需加引号）")
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if not _BARE_KEY_RE.fullmatch(str(sub_key)):
                    problems.append(f"{key}.{sub_key}（键名需加引号）")
                problems += _unfaithful_value(f"{key}.{sub_key}", sub_value)
        else:
            problems += _unfaithful_value(str(key), value)
    return problems


def set_sync_dir(root: Path, directory: Path) -> None:
    """合并式写回：保留既有其他配置段，原子替换（临时文件 + os.replace）。

    写前做保真校验：既有内容含无法被 _dump_toml 无损往返的形态
    （嵌套段/日期/表数组/需引号的键名）或解析失败时抛 ConfigWriteError
    拒写，不触碰原文件（校验在 os.replace 之前，无副作用）。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data, fatal = _read_toml(root)
    if fatal:
        raise ConfigWriteError(
            "config.toml 解析失败，已拒绝写入以免丢失现有内容；"
            "请修复语法后重试，或手工维护 [sync] 段的 dir 键")
    data = dict(data)
    section = data.get("sync")
    section = dict(section) if isinstance(section, dict) else {}
    section["dir"] = str(Path(directory))
    data["sync"] = section
    problems = _unfaithful(data)
    if problems:
        raise ConfigWriteError(
            "config.toml 含无法保真保留的内容：" + "、".join(problems)
            + "；已拒绝写入以免损坏你的文件，请手工维护 [sync] 段的 dir 键")
    path = root / CONFIG_NAME
    tmp = root / (CONFIG_NAME + ".tmp")
    tmp.write_text(_dump_toml(data), encoding="utf-8")
    os.replace(tmp, path)
