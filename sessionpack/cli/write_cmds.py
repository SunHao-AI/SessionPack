"""cli 的写入型命令：import / export / push / pull / delete。

业务编排集中在 run_xxx（不 print），cmd_xxx 只负责调用 emit 渲染并返回退出码。
"""

from __future__ import annotations

import getpass
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sessionpack import archive, catalog, config, export, importer, sync
from sessionpack.adapters import get_adapter
from sessionpack.audit import ReadAudit, ReadBoundaryError
from sessionpack.catalog.conn import CatalogSchemaError, CatalogVersionError
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
from sessionpack.rebuild import rebuild_from_archives, rebuild_fts_index


def run_import(args) -> CommandResult:
    lay = layout(args)
    if args.archive:
        conn = catalog.connect(lay.catalog)
        try:
            catalog.init_schema(conn)
            result = archive.import_archive(conn, Path(args.archive))
        except archive.ArchiveError as exc:
            return CommandResult(EXIT_VERIFY, err_lines=[
                "归档校验失败: " + ", ".join(issue.code for issue in exc.issues)])
        except (CatalogVersionError, CatalogSchemaError) as exc:
            return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
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
    except (CatalogVersionError, CatalogSchemaError) as exc:
        return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
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


def _missing_dir_errors(lay) -> list[str]:
    """「未指定同步目录」文案 + config 诊断（拼错段名等），使提示自洽。"""
    _, warnings = config.load_config(lay.root)
    return ["未指定同步目录：请加 --dir DIR，"
            "或先运行 sessionpack push --dir DIR 落配置"] + warnings


def _passphrase(prompt: str) -> str | None:
    """口令采集（D4）：环境变量优先（脚本/计划任务静默非交互），
    否则 getpass 两次一致。空口令或两次不一致返回 None，由调用方出
    EXIT_USAGE 提示；口令只经内存，不落盘、不进日志。"""
    env = os.environ.get("SESSIONPACK_PASSPHRASE")
    if env:
        return env
    first = getpass.getpass(prompt)
    if not first:
        return None
    if getpass.getpass("再次输入确认: ") != first:
        return None
    return first


def run_push(args) -> CommandResult:
    lay = layout(args)
    target = resolve_sync_dir(args, lay)
    if target is None:
        return CommandResult(EXIT_USAGE, err_lines=_missing_dir_errors(lay))
    # 三态：--encrypt / --no-encrypt 显式覆盖，未表态读 [sync].encrypt（D7）
    encrypt = config.sync_encrypt(lay.root) if args.encrypt is None \
        else args.encrypt
    passphrase = _passphrase("同步加密口令: ") if encrypt else None
    if encrypt and passphrase is None:
        return CommandResult(EXIT_USAGE,
                             err_lines=["已取消：加密推送需要口令"
                                        "（两次输入为空或不一致）"])
    result = sync.push_archives(lay.archives, target, dry_run=args.dry_run,
                                encrypt=encrypt, passphrase=passphrase)
    payload = {
        "sync_dir": str(target),
        "encrypt": encrypt,
        "pushed": result.pushed,
        "skipped_existing": result.skipped_existing,
        "skipped_unverified": list(result.skipped_unverified),
        "bytes": result.bytes,
        "errors": list(result.errors),
    }
    prefix = "[dry-run] " if args.dry_run else ""
    verb = "将推送" if args.dry_run else "已推送"
    mode = "（加密）" if encrypt else ""
    lines = [f"{prefix}{verb} {result.pushed} 个包共 {result.bytes} 字节{mode}，"
             f"跳过已存在 {result.skipped_existing}，"
             f"跳过未通过校验 {len(result.skipped_unverified)}"]
    lines += [f"跳过未通过校验的包: {item}" for item in result.skipped_unverified]
    errors = [f"推送失败: {item}" for item in result.errors]
    if result.errors:
        return CommandResult(EXIT_ERROR, out_lines=lines, err_lines=errors, data=payload)
    # 落配置对 JSON 与文本两种模式同样生效，与原文一致；encrypt 沿用 dir 的
    # 既有行为：显式指定过（args.encrypt 非 None）且非 dry-run 时持久化（D7）
    if not args.dry_run:
        try:
            if args.dir:
                config.set_sync_dir(lay.root, target)
            if args.encrypt is not None:
                config.set_sync_encrypt(lay.root, args.encrypt)
        except config.ConfigWriteError as exc:
            # 推送已成功这一事实仍要报告，只有"落配置"这一步失败
            return CommandResult(EXIT_ERROR, out_lines=lines, data=payload,
                                 err_lines=[f"落配置失败: {exc}"])
    return CommandResult(EXIT_OK, out_lines=lines, data=payload)


def cmd_push(args) -> int:
    return emit(run_push(args), args.json)


def run_pull(args) -> CommandResult:
    lay = layout(args)
    target = resolve_sync_dir(args, lay)
    if target is None:
        return CommandResult(EXIT_USAGE, err_lines=_missing_dir_errors(lay))
    if not target.is_dir():
        return CommandResult(EXIT_USAGE, err_lines=[f"同步目录不存在: {target}"])
    # 自适应：目录无 .spk 则完全不触碰口令（D4/D7）
    envelopes = [p for p in target.glob("*.spk") if not p.name.startswith(".")]
    passphrase = None
    if envelopes:
        passphrase = _passphrase("同步加密口令: ")
        if passphrase is None:
            return CommandResult(EXIT_USAGE,
                                 err_lines=[f"同步目录含 {len(envelopes)} 个加密信封，"
                                            "需要口令（两次输入为空或不一致，"
                                            "或设置 SESSIONPACK_PASSPHRASE）"])
    conn = catalog.connect(lay.catalog)
    try:
        catalog.init_schema(conn)
        result = sync.pull_archives(lay.archives, target, conn,
                                    encrypt=bool(envelopes),
                                    passphrase=passphrase)
    except (CatalogVersionError, CatalogSchemaError) as exc:
        return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
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


def run_rebuild(args) -> CommandResult:
    lay = layout(args)
    if getattr(args, "from_archives", False):
        if not getattr(args, "yes", False):
            return CommandResult(EXIT_USAGE, err_lines=[
                "rebuild --from-archives 会丢弃仅存在于记忆树、未归档的会话，"
                "请加 --yes 确认"])
        report = rebuild_from_archives(lay, assume_yes=True)
        if not report.ok:
            return CommandResult(EXIT_ERROR, err_lines=[report.error or "重建失败"])
        if args.json:
            return CommandResult(EXIT_OK, data=report.as_dict())
        lines = [f"全量重建完成：归档 {len(report.packages)} 个，"
                 f"会话 {report.sessions}，消息 {report.messages}，"
                 f"FTS {report.fts_rows} 行"]
        for item in report.packages:
            lines.append(f"  {item['name']}：会话 {item['sessions']}，"
                         f"消息 {item['messages']}")
        if report.backup:
            lines.append(f"原索引备份：{report.backup}")
        return CommandResult(EXIT_OK, out_lines=lines)

    # --fts 缺省模式：库不存在属用法错误，不隐式建库
    if not require_catalog(lay.catalog):
        return CommandResult(EXIT_USAGE,
                             err_lines=["索引不存在，请先运行 sessionpack import"])
    conn = catalog.connect(lay.catalog)
    try:
        rebuilt, recon = rebuild_fts_index(conn)
    except (CatalogVersionError, CatalogSchemaError) as exc:
        return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
    except sqlite3.Error as exc:
        return CommandResult(EXIT_ERROR, err_lines=[f"数据库错误: {exc}"])
    finally:
        conn.close()
    if args.json:
        return CommandResult(EXIT_OK, data={"rebuilt": rebuilt, "fts": recon})
    return CommandResult(EXIT_OK, out_lines=[f"FTS 重建完成：{rebuilt} 条"])


def cmd_rebuild(args) -> int:
    return emit(run_rebuild(args), args.json)


def _run_purge(conn, lay, args, status: str) -> CommandResult:
    """delete --purge --yes 的编排（Spec 13 D4）：先库后包、单包失败不回滚。

    调用方 run_delete 已完成索引存在性、会话存在性（status 非 None）与连接管理。
    """
    if getattr(args, "undo", False):
        return CommandResult(EXIT_USAGE,
                             err_lines=["--purge 与 --undo 不能同时使用"])
    if not getattr(args, "yes", False):
        return CommandResult(EXIT_USAGE, err_lines=[
            "--purge 会物理抹除本机内容且不可逆，请加 --yes 确认"])
    if status == "active":
        return CommandResult(EXIT_USAGE, err_lines=[
            f"需先软删除再清除: sessionpack delete --session {args.session}"])
    # status 为 deleted 或 purged（G2：只从 deleted 进；purged 走幂等成功）
    messages_removed = (len(catalog.get_messages(conn, args.session))
                        if status == "deleted" else 0)
    if status == "deleted":
        catalog.purge_session(
            conn, args.session,
            now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        conn.commit()
    repacked: list[str] = []
    failed: list[str] = []
    for name in sync.find_archives_with_session(lay.archives, args.session):
        try:
            new = archive.repak_excluding_session(
                lay.archives / name, args.session, lay.archives)
            try:
                # Windows 下旧包可能被占用，删除失败只记入 failed，不中断其余包
                (lay.archives / name).unlink()
            except OSError as exc:
                failed.append(f"{name}: 旧包删除失败: {exc}")
                continue
        except (archive.ArchiveError, archive.RepakError, OSError) as exc:
            failed.append(f"{name}: {exc}")
            continue
        repacked.append(new.name)
    sync_dir = resolve_sync_dir(args, lay)
    sync_stale = (sync.find_archives_with_session(sync_dir, args.session)
                  if sync_dir else [])
    data = {"session_id": args.session, "purged": True,
            "messages_removed": messages_removed,
            "archives_repacked": len(repacked),
            "archives_failed": failed, "sync_stale": sync_stale}
    lines = [f"已清除会话 {args.session}：抹除消息 {messages_removed} 条，"
             f"重打包 {len(repacked)} 个"]
    lines += [f"重打包失败（旧包原样在场）: {item}" for item in failed]
    lines += [f"同步目录仍含旧包（本命令不改动，请手动处理）: {item}"
              for item in sync_stale]
    if failed:
        return CommandResult(EXIT_ERROR, out_lines=lines, data=data)
    return CommandResult(EXIT_OK, out_lines=lines, data=data)


def run_delete(args) -> CommandResult:
    lay = layout(args)
    if not require_catalog(lay.catalog):
        return CommandResult(EXIT_USAGE,
                             err_lines=["索引不存在，请先运行 sessionpack import"])
    conn = catalog.connect(lay.catalog)
    try:
        catalog.init_schema(conn)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        status = catalog.get_session_status(conn, args.session)
        if status is None:
            return CommandResult(EXIT_USAGE,
                                 err_lines=[f"会话不存在: {args.session}"])
        if getattr(args, "purge", False):
            return _run_purge(conn, lay, args, status)
        if args.undo:
            if status != "deleted":
                return CommandResult(
                    EXIT_USAGE,
                    err_lines=[f"会话未处于删除状态，无需撤销: {args.session}"])
            catalog.restore_session(conn, args.session, now=now)
            conn.commit()
            line = (f"已撤销删除：会话 {args.session} 已恢复（仅本机生效；"
                    "其他设备上已导入的墓碑不受影响，需在其上各自撤销）")
        else:
            catalog.mark_session_deleted(conn, args.session, now=now)
            conn.commit()
            line = (f"已删除会话 {args.session}（本机视图隐藏；"
                    "export --archive + push 后，删除将随归档包传播且不会因旧包复活）")
        if args.json:
            return CommandResult(EXIT_OK, data={
                "session_id": args.session, "undo": bool(args.undo), "ok": True})
        return CommandResult(EXIT_OK, out_lines=[line])
    except (CatalogVersionError, CatalogSchemaError) as exc:
        return CommandResult(EXIT_ERROR, err_lines=[str(exc)])
    except sqlite3.Error as exc:
        return CommandResult(EXIT_ERROR, err_lines=[f"数据库错误: {exc}"])
    finally:
        conn.close()


def cmd_delete(args) -> int:
    return emit(run_delete(args), args.json)
