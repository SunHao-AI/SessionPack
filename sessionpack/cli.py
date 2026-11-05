from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sessionpack import __version__, archive, catalog, diagnostics, export, importer
from sessionpack.adapters import get_adapter
from sessionpack.audit import ReadBoundaryError, ReadAudit
from sessionpack.paths import Layout, default_memory_root, ensure_layout, resolve_data_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3


def _layout(args) -> Layout:
    return ensure_layout(resolve_data_dir(args.data_dir))


def _memory_root(args) -> Path:
    return Path(args.memory_root) if args.memory_root else default_memory_root()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sessionpack",
                                     description="Trae 会话归档与同步：本地只读管理")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查数据源、索引与私有库状态")
    doctor.add_argument("--memory-root", default=None)
    doctor.add_argument("--data-dir", default=None)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    scan = sub.add_parser("scan", help="扫描记忆目录，不写索引")
    scan.add_argument("--memory-root", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=_cmd_scan)

    imp = sub.add_parser("import", help="导入记忆目录或归档包")
    imp.add_argument("--memory-root", default=None)
    imp.add_argument("--data-dir", default=None)
    imp.add_argument("--archive", default=None)
    imp.add_argument("--project", default=None)
    imp.add_argument("--since", default=None)
    imp.add_argument("--until", default=None)
    imp.add_argument("--reparse", action="store_true")
    imp.add_argument("--audit", action="store_true")
    imp.add_argument("--json", action="store_true")
    imp.set_defaults(func=_cmd_import)

    serve = sub.add_parser("serve", help="启动本地 Web UI")
    serve.add_argument("--memory-root", default=None)
    serve.add_argument("--data-dir", default=None)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=_cmd_serve)

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
    exp.set_defaults(func=_cmd_export)

    ver = sub.add_parser("verify", help="校验归档包")
    ver.add_argument("archives", nargs="+")
    ver.add_argument("--json", action="store_true")
    ver.set_defaults(func=_cmd_verify)
    return parser


def _cmd_doctor(args) -> int:
    layout = _layout(args)
    data = diagnostics.collect(_memory_root(args), layout)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"SessionPack {__version__}")
        print(f"记忆目录：{data['memory_root']}"
              f"（{'存在' if data['memory_root_exists'] else '不存在'}）")
        source = data["source"]
        if source:
            print(f"适配器：{source['adapter_id']} v{source['adapter_version']}")
            print(f"项目 {source['projects']} 个（目录 {source['project_dirs']} 个）")
            print(f"topics.md {source['topic_files']} 个，"
                  f"jsonl {source['jsonl_files']} 个，{source['bytes_total']} 字节")
            if source["date_min"]:
                print(f"日期范围：{source['date_min']} 至 {source['date_max']}")
        private = data["private_db"]
        print(f"私有库：{private['path']}"
              f"（{'存在' if private['exists'] else '不存在'}，"
              f"{private['size']} 字节，不读取）")
        print(f"索引：{data['catalog']['path']}"
              f"（{'存在' if data['catalog']['exists'] else '未创建'}）")
        stats = data["catalog"]["stats"]
        print(f"索引统计：项目 {stats['projects']}，会话 {stats['sessions']}，"
              f"消息 {stats['messages']}，FTS {stats['fts_rows']}")
    return EXIT_OK if data["memory_root_exists"] else EXIT_USAGE


def _cmd_scan(args) -> int:
    root = _memory_root(args)
    if not root.is_dir():
        print(f"记忆目录不存在: {root}", file=sys.stderr)
        return EXIT_USAGE
    info = get_adapter().inspect(root)
    if args.json:
        print(json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"适配器：{info.adapter_id} v{info.adapter_version}")
        print(f"项目 {info.project_count} 个（目录 {info.project_dir_count} 个）")
        print(f"topics.md {info.topic_files} 个，jsonl {info.jsonl_files} 个，"
              f"{info.bytes_total} 字节")
        print(f"日期范围：{info.date_min} 至 {info.date_max}")
    return EXIT_OK


def _cmd_import(args) -> int:
    layout = _layout(args)
    if args.archive:
        conn = catalog.connect(layout.catalog)
        catalog.init_schema(conn)
        try:
            result = archive.import_archive(conn, Path(args.archive))
        except archive.ArchiveError as exc:
            print("归档校验失败: " + ", ".join(issue.code for issue in exc.issues),
                  file=sys.stderr)
            return EXIT_VERIFY
        finally:
            conn.close()
        print(f"归档 {result.archive_id}：会话 {result.sessions}，消息 {result.messages}，"
              f"新增消息 {result.messages_inserted}")
        return EXIT_OK

    root = _memory_root(args)
    if not root.is_dir():
        print(f"记忆目录不存在: {root}", file=sys.stderr)
        return EXIT_USAGE
    adapter = get_adapter()
    declared = adapter.declared_paths(root)
    kwargs = dict(reparse=args.reparse, project_filter=args.project,
                  since=args.since, until=args.until)
    if args.audit:
        with ReadAudit(root, declared) as audit:
            stats = importer.import_source(adapter, root, layout.catalog, **kwargs)
        try:
            audit.check(declared)
        except ReadBoundaryError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        if not args.json:
            print(f"读取边界：审计通过（打开 {len(audit.opened)} 个文件）")
    else:
        stats = importer.import_source(adapter, root, layout.catalog, **kwargs)

    if args.json:
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"导入完成：文件 {stats.files_processed}/{stats.files_declared}，"
              f"跳过 {stats.files_skipped}，消息 {stats.messages_inserted}，"
              f"主题 {stats.topics_applied}，失败 {stats.rows_failed} 行，"
              f"用时 {stats.elapsed_s:.2f}s")
        if stats.stopped:
            print("已按停止条件中断，下次运行将自动续接。")
    return EXIT_OK


def _cmd_serve(args) -> int:
    layout = _layout(args)
    if not layout.catalog.is_file():
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    from sessionpack.server import serve

    serve(layout.catalog, port=args.port, open_browser=not args.no_browser,
          memory_root=_memory_root(args))
    return EXIT_OK


def _cmd_export(args) -> int:
    layout = _layout(args)
    if not layout.catalog.is_file():
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    conn = catalog.connect(layout.catalog)
    out = Path(args.out)
    try:
        if args.archive:
            if args.session:
                print("归档不支持按会话生成，请用 --project 或 --all", file=sys.stderr)
                return EXIT_USAGE
            project_ids = ([args.project] if args.project
                           else [row["project_id"] for row in catalog.get_projects(conn)])
            for project_id in project_ids:
                item = archive.build_archive(conn, out, project_id)
                print(f"已生成 {item.path}（archive_id {item.archive_id}，"
                      f"会话 {item.sessions}，消息 {item.messages}）")
            return EXIT_OK
        if args.session:
            path = export.export_session(conn, args.session, out, args.format)
            print(f"已导出 {path}")
            return EXIT_OK
        if args.project:
            result = export.export_project(conn, args.project, out, args.format)
        else:
            result = export.export_all(conn, out, args.format)
        print(f"已导出 {result.sessions} 个会话、{result.messages} 条消息到 {out}")
        return EXIT_OK
    except KeyError:
        print(f"会话不存在: {args.session}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        conn.close()


def _cmd_verify(args) -> int:
    results = [archive.verify_archive(Path(raw)) for raw in args.archives]
    if args.json:
        print(json.dumps([
            {"path": str(result.path), "ok": result.ok,
             "issues": [{"code": issue.code, "detail": issue.detail}
                        for issue in result.issues]}
            for result in results
        ], ensure_ascii=False, indent=2))
    else:
        for result in results:
            if result.ok:
                print(f"OK  {result.path}"
                      f"（archive_id {result.manifest['archive_id']}）")
            else:
                print(f"BAD {result.path}："
                      + ", ".join(issue.code for issue in result.issues))
    return EXIT_OK if all(result.ok for result in results) else EXIT_VERIFY


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
