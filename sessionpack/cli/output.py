"""cli 的渲染层：业务函数只产出 CommandResult，打印与退出码在此统一决定。

本模块在 lint_imports 中登记为 L0：只依赖标准库，不 import 任何 sessionpack
业务模块，因此任何命令模块都能安全依赖它。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommandResult:
    """一条命令的渲染输入：退出码 + 文本行 + JSON 载荷。"""

    exit_code: int
    out_lines: list[str] = field(default_factory=list)
    err_lines: list[str] = field(default_factory=list)
    data: Any | None = None


def emit(res: CommandResult, as_json: bool) -> int:
    """渲染结果并返回退出码。

    as_json 且 data 非 None 时只输出 JSON；否则输出文本行（stdout）与错误行
    （stderr）。data 为 None 时 as_json 不生效，恒走文本/错误行。
    """
    if as_json and res.data is not None:
        print(json.dumps(res.data, ensure_ascii=False, indent=2))
    else:
        for line in res.out_lines:
            print(line)
        for line in res.err_lines:
            print(line, file=sys.stderr)
    return res.exit_code
