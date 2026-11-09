"""cli 包门面：保持拆分前 sessionpack.cli.X 的全部可导入名字。

子模块归位：parser=参数定义，common=常量与助手，output=渲染（L0），
read_cmds=只读命令，write_cmds=写入命令。
main 按 args.command 在门面命名空间里查 _cmd_*，使 monkeypatch 仍可生效。
"""

from __future__ import annotations

import sys

from sessionpack.cli.common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY,
)
from sessionpack.cli.parser import build_parser
from sessionpack.cli.read_cmds import (
    cmd_doctor,
    cmd_scan,
    cmd_search,
    cmd_serve,
    cmd_verify,
)
from sessionpack.cli.write_cmds import (
    cmd_delete,
    cmd_export,
    cmd_import,
    cmd_pull,
    cmd_push,
    cmd_rebuild,
    cmd_reencrypt,
)

_cmd_doctor = cmd_doctor
_cmd_scan = cmd_scan
_cmd_import = cmd_import
_cmd_serve = cmd_serve
_cmd_export = cmd_export
_cmd_verify = cmd_verify
_cmd_push = cmd_push
_cmd_pull = cmd_pull
_cmd_search = cmd_search
_cmd_rebuild = cmd_rebuild
_cmd_reencrypt = cmd_reencrypt
_cmd_delete = cmd_delete

__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VERIFY",
    "build_parser",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = globals()[f"_cmd_{args.command}"]
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 与拆分前一致：最外层兜底成一行错误
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
