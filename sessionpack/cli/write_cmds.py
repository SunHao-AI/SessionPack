"""cli 的写入型命令：import / export / push / pull。

业务编排集中在 run_xxx（不 print），cmd_xxx 只负责调用 emit 渲染并返回退出码。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sessionpack import archive, catalog, config, export, importer, sync
from sessionpack.adapters import get_adapter
from sessionpack.audit import ReadAudit, ReadBoundaryError
from sessionpack.cli.common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY,
    layout,
    memory_root,
    require_catalog,
    resolve_sync_dir,
)
from sessionpack.cli.output import CommandResult, emit


def run_import(args) -> CommandResult:
    lay = layout(args)
    if args.archive:
        conn = catalog.connect(lay.catalog)
        catalog.init_schema(conn)
        try:
            result = archive.import_archive(conn, Path(args.archive))
        except archive.ArchiveError as exc:
            return CommandResult(EXIT_VERIFY, err_lines=[
                "归档校验失败: " + ", ".join(issue.code for issue in exc.issues)])
        except sqlite3.Error as exc:
            return CommandResult(EXIT_ERROR, err_lines=[f"数据库错误: {exc}"])
        finally:
            conn.close()
        return CommandResult(EXIT_OK, out_lines=[
            f"归档 {result.archive_id}：会话 {result.sessions}，"
            f"消息 {result.messages}，新增消息 {result.messages_inserted}"])

    root = memory_root(args)
    if not root.is_dir():
        return CommandResult(EXIT_USAGE, err_lines=[f"记忆目录不存在: {root}"])
    adapter = get_adapter()
    declared = adapter.declared_paths(root)
    kwargs = dict(reparse=args.reparse, retry_failed=args.retry_failed,
                  project_filter=args.project,
                  since=args.since, until=args.until)
    boundary_lines: list[str] = []
    try:
        if args.audit:
            with ReadAudit(root, declared) as audit:
                stats = importer.import_source(adapter, root, lay.catalog, **kwargs)
            try:
                audit.check(declared)
            except ReadBoundaryError as exc:
                return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
            if not args.json:
                boundary_lines.append(
                    f"读取边界：审计通过（打开 {len(audit.opened)} 个文件）")
        else:
            stats = importer.import_source(adapter, root, lay.catalog, **kwargs)
    except sqlite3.Error as exc:
        return CommandResult(EXIT_ERROR, err_lines=[f"数据库错误: {exc}"])

    if args.json:
        return CommandResult(EXIT_OK, data=stats.as_dict())
    lines = [
        f"导入完成：文件 {stats.files_processed}/{stats.files_declared}，"
        f"跳过 {stats.files_skipped}，消息 {stats.messages_inserted}，"
        f"主题 {stats.topics_applied}，失败 {stats.rows_failed} 行，"
        f"用时 {stats.elapsed_s:.2f}s"
    ]
    if stats.stopped:
        lines.append("已按停止条件中断，下次运行将自动续接。")
    return CommandResult(EXIT_OK, out_lines=boundary_lines + lines)


def cmd_import(args) -> int:
    return emit(run_import(args), args.json)


def run_export(args) -> CommandResult:
    lay = layout(args)
    if not require_catalog(lay.catalog):
        return CommandResult(EXIT_USAGE,
                             err_lines=["索引不存在，请先运行 sessionpack import"])
    conn = catalog.connect(lay.catalog)
    out = Path(args.out)
    try:
        if args.archive:
            if args.session:
                return CommandResult(
                    EXIT_USAGE,
                    err_lines=["归档不支持按会话生成，请用 --project 或 --all"])
            project_ids = ([args.project] if args.project
                           else [row["project_id"]
                                 for row in catalog.get_projects(conn)])
            lines = []
            for project_id in project_ids:
                item = archive.build_archive(conn, out, project_id)
                lines.append(f"已生成 {item.path}（archive_id {item.archive_id}，"
                             f"会话 {item.sessions}，消息 {item.messages}）")
            return CommandResult(EXIT_OK, out_lines=lines)
        if args.session:
            try:
                path, _ = export.export_session(conn, args.session, out, args.format)
            except KeyError:
                return CommandResult(EXIT_USAGE,
                                     err_lines=[f"会话不存在: {args.session}"])
            return CommandResult(EXIT_OK, out_lines=[f"已导出 {path}"])
        if args.project:
            if not catalog.list_sessions(conn, project_id=args.project, limit=1):
                return CommandResult(
                    EXIT_USAGE, err_lines=[f"项目不存在或无会话: {args.project}"])
            result = export.export_project(conn, args.project, out, args.format)
        else:
            result = export.export_all(conn, out, args.format)
        return CommandResult(EXIT_OK, out_lines=[
            f"已导出 {result.sessions} 个会话、{result.messages} 条消息到 {out}"])
    except ValueError as exc:
        return CommandResult(EXIT_ERROR, err_lines=[f"导出失败: {exc}"])
    except sqlite3.Error as exc:
        return CommandResult(EXIT_ERROR, err_lines=[f"数据库错误: {exc}"])
    finally:
        conn.close()


def cmd_export(args) -> int:
    # export 没有 --json 参数，恒走文本渲染
    return emit(run_export(args), False)


def run_push(args) -> CommandResult:
    lay = layout(args)
    target = resolve_sync_dir(args, lay)
    if target is None:
        return CommandResult(
            EXIT_USAGE,
            err_lines=["未指定同步目录：请加 --dir DIR，"
                       "或先运行 sessionpack push --dir DIR 落配置"])
    result = sync.push_archives(lay.archives, target, dry_run=args.dry_run)
    payload = {
        "sync_dir": str(target),
        "pushed": result.pushed,
        "skipped_existing": result.skipped_existing,
        "skipped_unverified": list(result.skipped_unverified),
        "bytes": result.bytes,
        "errors": list(result.errors),
    }
    prefix = "[dry-run] " if args.dry_run else ""
    verb = "将推送" if args.dry_run else "已推送"
    lines = [f"{prefix}{verb} {result.pushed} 个包共 {result.bytes} 字节，"
             f"跳过已存在 {result.skipped_existing}，"
             f"跳过未通过校验 {len(result.skipped_unverified)}"]
    lines += [f"跳过未通过校验的包: {item}" for item in result.skipped_unverified]
    errors = [f"推送失败: {item}" for item in result.errors]
    if result.errors:
        return CommandResult(EXIT_ERROR, out_lines=lines, err_lines=errors, data=payload)
    # 落配置对 JSON 与文本两种模式同样生效，与原文一致
    if args.dir and not args.dry_run:
        config.set_sync_dir(lay.root, target)
    return CommandResult(EXIT_OK, out_lines=lines, data=payload)


def cmd_push(args) -> int:
    return emit(run_push(args), args.json)


def run_pull(args) -> CommandResult:
    lay = layout(args)
    target = resolve_sync_dir(args, lay)
    if target is None:
        return CommandResult(
            EXIT_USAGE,
            err_lines=["未指定同步目录：请加 --dir DIR，"
                       "或先运行 sessionpack push --dir DIR 落配置"])
    if not target.is_dir():
        return CommandResult(EXIT_USAGE, err_lines=[f"同步目录不存在: {target}"])
    conn = catalog.connect(lay.catalog)
    catalog.init_schema(conn)
    try:
        result = sync.pull_archives(lay.archives, target, conn)
    finally:
        conn.close()
    if args.json:
        return CommandResult(EXIT_ERROR if result.errors else EXIT_OK, data={
            "sync_dir": str(target),
            "pulled": result.pulled,
            "skipped_existing": result.skipped_existing,
            "skipped_invalid": list(result.skipped_invalid),
            "sessions_added": result.sessions_added,
            "messages_added": result.messages_added,
            "bytes": result.bytes,
            "errors": list(result.errors),
        })
    lines = [f"已拉取 {result.pulled} 个包（新增会话 {result.sessions_added}、"
             f"消息 {result.messages_added}），跳过已存在 {result.skipped_existing}，"
             f"跳过无效包 {len(result.skipped_invalid)}"]
    lines += [f"跳过无效包: {item}" for item in result.skipped_invalid]
    if result.errors:
        return CommandResult(EXIT_ERROR, out_lines=lines,
                             err_lines=[f"拉取失败: {item}" for item in result.errors])
    return CommandResult(EXIT_OK, out_lines=lines)


def cmd_pull(args) -> int:
    return emit(run_pull(args), args.json)
