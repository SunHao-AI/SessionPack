"""子命令与参数定义：不含业务逻辑，也不引用任何命令实现。"""

from __future__ import annotations

import argparse

from sessionpack import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sessionpack",
                                     description="Trae 会话归档与同步：本地只读管理")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查数据源、索引与私有库状态")
    doctor.add_argument("--memory-root", default=None)
    doctor.add_argument("--data-dir", default=None)
    doctor.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan", help="扫描记忆目录，不写索引")
    scan.add_argument("--memory-root", default=None)
    scan.add_argument("--json", action="store_true")

    imp = sub.add_parser("import", help="导入记忆目录或归档包")
    imp.add_argument("--memory-root", default=None)
    imp.add_argument("--data-dir", default=None)
    imp.add_argument("--archive", default=None)
    imp.add_argument("--project", default=None)
    imp.add_argument("--since", default=None)
    imp.add_argument("--until", default=None)
    imp.add_argument("--reparse", action="store_true")
    imp.add_argument("--retry-failed", action="store_true",
                     help="重置上次失败的源文件并重新读取")
    imp.add_argument("--audit", action="store_true")
    imp.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="启动本地 Web UI")
    serve.add_argument("--memory-root", default=None)
    serve.add_argument("--data-dir", default=None)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")

    exp = sub.add_parser("export", help="导出会话为 Markdown 或 JSON")
    exp.add_argument("--memory-root", default=None)
    exp.add_argument("--data-dir", default=None)
    target = exp.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", default=None)
    target.add_argument("--project", default=None)
    target.add_argument("--all", action="store_true")
    exp.add_argument("--out", required=True)
    exp.add_argument("--format", choices=("markdown", "json"), default="markdown")
    exp.add_argument("--archive", action="store_true", help="生成 zip 归档包")

    ver = sub.add_parser("verify", help="校验归档包")
    ver.add_argument("archives", nargs="+")
    ver.add_argument("--json", action="store_true")

    push = sub.add_parser("push", help="推送本地归档包到同步目录（只增不删）")
    push.add_argument("--dir", default=None, help="本次使用的同步目录（缺省读配置）")
    push.add_argument("--dry-run", action="store_true",
                      help="只列出将推送的文件与字节数，不写入任何东西")
    push.add_argument("--data-dir", default=None)
    push.add_argument("--json", action="store_true")

    pull = sub.add_parser("pull", help="从同步目录拉取归档包并导入索引（只增不删）")
    pull.add_argument("--dir", default=None, help="本次使用的同步目录（缺省读配置）")
    pull.add_argument("--data-dir", default=None)
    pull.add_argument("--json", action="store_true")
    return parser
