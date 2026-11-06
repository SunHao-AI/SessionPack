from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sessionpack import __version__, archive, catalog, config, diagnostics, export, importer, sync
from sessionpack.adapters import get_adapter
from sessionpack.audit import ReadAudit, ReadBoundaryError
from sessionpack.paths import Layout, default_memory_root, ensure_layout, resolve_data_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3


def _layout(args) -> Layout:
    return ensure_layout(resolve_data_dir(args.data_dir))


def _memory_root(args) -> Path:
    return Path(args.memory_root) if args.memory_root else default_memory_root()


def _catalog_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True
        )
    except sqlite3.Error:
        return False
    try:
        return catalog.has_schema(conn)
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


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
    imp.add_argument("--retry-failed", action="store_true",
                     help="重置上次失败的源文件并重新读取")
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

    push = sub.add_parser("push", help="推送本地归档包到同步目录（只增不删）")
    push.add_argument("--dir", default=None, help="本次使用的同步目录（缺省读配置）")
    push.add_argument("--dry-run", action="store_true",
                      help="只列出将推送的文件与字节数，不写入任何东西")
    push.add_argument("--data-dir", default=None)
    push.add_argument("--json", action="store_true")
    push.set_defaults(func=_cmd_push)

    pull = sub.add_parser("pull", help="从同步目录拉取归档包并导入索引（只增不删）")
    pull.add_argument("--dir", default=None, help="本次使用的同步目录（缺省读配置）")
    pull.add_argument("--data-dir", default=None)
    pull.add_argument("--json", action="store_true")
    pull.set_defaults(func=_cmd_pull)
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
        catalog_info = data["catalog"]
        state = "未创建"
        if catalog_info["exists"]:
            state = "存在" if catalog_info["ready"] else "存在，索引未初始化"
        print(f"索引：{catalog_info['path']}（{state}）")
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
        except sqlite3.Error as exc:
            print(f"数据库错误: {exc}", file=sys.stderr)
            return EXIT_ERROR
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
    kwargs = dict(reparse=args.reparse, retry_failed=args.retry_failed,
                  project_filter=args.project,
                  since=args.since, until=args.until)
    try:
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
    except sqlite3.Error as exc:
        print(f"数据库错误: {exc}", file=sys.stderr)
        return EXIT_ERROR

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
    if not _catalog_ready(layout.catalog):
        print("索引不存在，请先运行 sessionpack import", file=sys.stderr)
        return EXIT_USAGE
    from sessionpack.server import serve

    serve(layout.catalog, port=args.port, open_browser=not args.no_browser,
          memory_root=_memory_root(args))
    return EXIT_OK


def _cmd_export(args) -> int:
    layout = _layout(args)
    if not _catalog_ready(layout.catalog):
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
            try:
                path, _ = export.export_session(conn, args.session, out, args.format)
            except KeyError:
                print(f"会话不存在: {args.session}", file=sys.stderr)
                return EXIT_USAGE
            print(f"已导出 {path}")
            return EXIT_OK
        if args.project:
            if not catalog.list_sessions(conn, project_id=args.project, limit=1):
                print(f"项目不存在或无会话: {args.project}", file=sys.stderr)
                return EXIT_USAGE
            result = export.export_project(conn, args.project, out, args.format)
        else:
            result = export.export_all(conn, out, args.format)
        print(f"已导出 {result.sessions} 个会话、{result.messages} 条消息到 {out}")
        return EXIT_OK
    except ValueError as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.Error as exc:
        print(f"数据库错误: {exc}", file=sys.stderr)
        return EXIT_ERROR
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
                assert result.manifest is not None  # verify_archive 成功时 manifest 必非空
                print(f"OK  {result.path}"
                      f"（archive_id {result.manifest['archive_id']}）")
            else:
                print(f"BAD {result.path}："
                      + ", ".join(issue.code for issue in result.issues))
    return EXIT_OK if all(result.ok for result in results) else EXIT_VERIFY


def _resolve_sync_dir(args, layout: Layout) -> Path | None:
    if args.dir:
        # 与 paths.resolve_data_dir 对齐：展开并解析为绝对路径后再落配置
        return Path(args.dir).expanduser().resolve()
    return config.sync_dir(layout.root)


def _cmd_push(args) -> int:
    layout = _layout(args)
    target = _resolve_sync_dir(args, layout)
    if target is None:
        print("未指定同步目录：请加 --dir DIR，或先运行 sessionpack push --dir DIR 落配置",
              file=sys.stderr)
        return EXIT_USAGE
    result = sync.push_archives(layout.archives, target, dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "sync_dir": str(target),
            "pushed": result.pushed,
            "skipped_existing": result.skipped_existing,
            "skipped_unverified": list(result.skipped_unverified),
            "bytes": result.bytes,
            "errors": list(result.errors),
        }, ensure_ascii=False, indent=2))
    else:
        prefix = "[dry-run] " if args.dry_run else ""
        verb = "将推送" if args.dry_run else "已推送"
        print(f"{prefix}{verb} {result.pushed} 个包共 {result.bytes} 字节，"
              f"跳过已存在 {result.skipped_existing}，"
              f"跳过未通过校验 {len(result.skipped_unverified)}")
        for item in result.skipped_unverified:
            print(f"跳过未通过校验的包: {item}")
        for item in result.errors:
            print(f"推送失败: {item}", file=sys.stderr)
    if result.errors:
        return EXIT_ERROR
    if args.dir and not args.dry_run:
        config.set_sync_dir(layout.root, target)
    return EXIT_OK


def _cmd_pull(args) -> int:
    layout = _layout(args)
    target = _resolve_sync_dir(args, layout)
    if target is None:
        print("未指定同步目录：请加 --dir DIR，或先运行 sessionpack push --dir DIR 落配置",
              file=sys.stderr)
        return EXIT_USAGE
    if not target.is_dir():
        print(f"同步目录不存在: {target}", file=sys.stderr)
        return EXIT_USAGE
    conn = catalog.connect(layout.catalog)
    catalog.init_schema(conn)
    try:
        result = sync.pull_archives(layout.archives, target, conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({
            "sync_dir": str(target),
            "pulled": result.pulled,
            "skipped_existing": result.skipped_existing,
            "skipped_invalid": list(result.skipped_invalid),
            "sessions_added": result.sessions_added,
            "messages_added": result.messages_added,
            "bytes": result.bytes,
            "errors": list(result.errors),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"已拉取 {result.pulled} 个包（新增会话 {result.sessions_added}、"
              f"消息 {result.messages_added}），跳过已存在 {result.skipped_existing}，"
              f"跳过无效包 {len(result.skipped_invalid)}")
        for item in result.skipped_invalid:
            print(f"跳过无效包: {item}")
        for item in result.errors:
            print(f"拉取失败: {item}", file=sys.stderr)
    return EXIT_ERROR if result.errors else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
