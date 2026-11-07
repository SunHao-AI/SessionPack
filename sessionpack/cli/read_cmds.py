"""cli 的只读型命令：doctor / scan / verify / serve。"""

from __future__ import annotations

import sys
from pathlib import Path

from sessionpack import __version__, archive, diagnostics
from sessionpack.adapters import get_adapter
from sessionpack.cli.common import (
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY,
    layout,
    memory_root,
    require_catalog,
)
from sessionpack.cli.output import CommandResult, emit


def run_doctor(args) -> CommandResult:
    data = diagnostics.collect(memory_root(args), layout(args))
    code = EXIT_OK if data["memory_root_exists"] else EXIT_USAGE
    if args.json:
        return CommandResult(code, data=data)
    lines = [
        f"SessionPack {__version__}",
        f"记忆目录：{data['memory_root']}"
        f"（{'存在' if data['memory_root_exists'] else '不存在'}）",
    ]
    source = data["source"]
    if source:
        lines.append(f"适配器：{source['adapter_id']} v{source['adapter_version']}")
        lines.append(f"项目 {source['projects']} 个（目录 {source['project_dirs']} 个）")
        lines.append(f"topics.md {source['topic_files']} 个，"
                     f"jsonl {source['jsonl_files']} 个，{source['bytes_total']} 字节")
        if source["date_min"]:
            lines.append(f"日期范围：{source['date_min']} 至 {source['date_max']}")
    private = data["private_db"]
    lines.append(f"私有库：{private['path']}"
                 f"（{'存在' if private['exists'] else '不存在'}，"
                 f"{private['size']} 字节，不读取）")
    catalog_info = data["catalog"]
    state = "未创建"
    if catalog_info["exists"]:
        state = "存在" if catalog_info["ready"] else "存在，索引未初始化"
    lines.append(f"索引：{catalog_info['path']}（{state}）")
    stats = catalog_info["stats"]
    lines.append(f"索引统计：项目 {stats['projects']}，会话 {stats['sessions']}，"
                 f"消息 {stats['messages']}，FTS {stats['fts_rows']}")
    if catalog_info["ready"]:
        fts = catalog_info["fts"]
        verdict = "一致" if not fts["drift"] else (
            f"漂移（缺失 {fts['fts_missing']}、多余 {fts['fts_extra']}）")
        lines.append(f"索引对账：messages {stats['messages']} ↔ "
                     f"message_fts {stats['fts_rows']}（{verdict}）")
        if fts["drift"]:
            lines.append("索引警告：可运行 sessionpack rebuild --fts 修复")
    sync = data["sync"]
    lines.append(f"同步目录：{sync['dir'] if sync['configured'] else '未配置'}")
    lines += [f"同步警告：{w}" for w in sync["warnings"]]
    return CommandResult(code, out_lines=lines)


def cmd_doctor(args) -> int:
    return emit(run_doctor(args), args.json)


def run_scan(args) -> CommandResult:
    root = memory_root(args)
    if not root.is_dir():
        return CommandResult(EXIT_USAGE, err_lines=[f"记忆目录不存在: {root}"])
    info = get_adapter().inspect(root)
    if args.json:
        return CommandResult(EXIT_OK, data=info.as_dict())
    return CommandResult(EXIT_OK, out_lines=[
        f"适配器：{info.adapter_id} v{info.adapter_version}",
        f"项目 {info.project_count} 个（目录 {info.project_dir_count} 个）",
        f"topics.md {info.topic_files} 个，jsonl {info.jsonl_files} 个，"
        f"{info.bytes_total} 字节",
        f"日期范围：{info.date_min} 至 {info.date_max}",
    ])


def cmd_scan(args) -> int:
    return emit(run_scan(args), args.json)


def run_verify(args) -> CommandResult:
    results = [archive.verify_archive(Path(raw)) for raw in args.archives]
    code = EXIT_OK if all(result.ok for result in results) else EXIT_VERIFY
    if args.json:
        return CommandResult(code, data=[
            {"path": str(result.path), "ok": result.ok,
             "issues": [{"code": issue.code, "detail": issue.detail}
                        for issue in result.issues]}
            for result in results
        ])
    lines: list[str] = []
    for result in results:
        if result.ok:
            assert result.manifest is not None  # verify_archive 成功时 manifest 必非空
            lines.append(f"OK  {result.path}"
                         f"（archive_id {result.manifest['archive_id']}）")
        else:
            lines.append(f"BAD {result.path}："
                         + ", ".join(issue.code for issue in result.issues))
    return CommandResult(code, out_lines=lines)


def cmd_verify(args) -> int:
    return emit(run_verify(args), args.json)


def cmd_serve(args) -> int:
    """serve 特例：阻塞在本地 HTTP 服务上，不产出 CommandResult。"""
    lay = layout(args)
    if not require_catalog(lay.catalog):
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    from sessionpack.server import serve

    serve(lay.catalog, port=args.port, open_browser=not args.no_browser,
          memory_root=memory_root(args))
    return EXIT_OK
