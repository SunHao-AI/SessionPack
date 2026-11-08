"""索引重建（Spec 7 §4/§5）：rebuild --fts 与 rebuild --from-archives 的编排。

定位：索引库是纯派生缓存，一致性问题的解药永远是重建（spec §1）。
本模块只做流程编排——全部写 SQL 仍收敛在 catalog/write.py。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sessionpack import archive, catalog
from sessionpack.catalog.conn import CatalogSchemaError, CatalogVersionError
from sessionpack.catalog.read import reconcile_fts
from sessionpack.catalog.write import clear_derived_rows, rebuild_fts
from sessionpack.paths import Layout


class RebuildError(RuntimeError):
    """库级兜底：程序化绕过 CLI 的 --yes 确认时拒绝执行。"""


@dataclass
class RebuildReport:
    ok: bool
    error: str | None = None
    packages: list[dict] = field(default_factory=list)
    sessions: int = 0
    messages: int = 0
    fts_rows: int = 0
    backup: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def rebuild_fts_index(conn: sqlite3.Connection) -> tuple[int, dict]:
    """--fts 模式：单事务全删重插 + 集合级自检，返回 (重建行数, 对账结果)。

    tokenizer 为 none（FTS 未建）时不做任何写，如实返回对账全零。
    """
    if catalog.tokenizer_of(conn) == "none":
        return 0, reconcile_fts(conn)
    rebuilt = rebuild_fts(conn)
    return rebuilt, reconcile_fts(conn)


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]


def _cleanup(path: Path) -> None:
    for extra in _sidecars(path):
        extra.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


_REPLACE_RETRIES = 5
_REPLACE_BACKOFF_S = 0.2


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace 的有限退避重试。

    Windows 上目标被他进程短时持有（如另一路重建正打开旧库）时
    os.replace 抛 PermissionError（WinError 32 共享冲突），
    退避后多半可成；重试耗尽原样上抛，交调用侧回滚。
    """
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S)


def _discard_quietly(path: Path) -> None:
    """失败路径的尽力清理：被并发重建者持有时留待下一轮入场清理。"""
    try:
        _cleanup(path)
    except OSError:
        pass


def rebuild_from_archives(layout: Layout, *, assume_yes: bool) -> RebuildReport:
    """从 layout.archives/*.zip 在 staging 上全量重建，成功后原子换入。

    会丢弃仅存在于记忆树、从未导出进任何归档的会话，故要求 assume_yes。
    安全不变量：换入前原库只读，任何失败路径原库分毫未动、无 staging 残留。
    """
    if not assume_yes:
        raise RebuildError("全量重建会丢弃未归档的会话，需显式确认（--yes）")
    archives = sorted(layout.archives.glob("*.zip"))
    if not archives:
        return RebuildReport(
            ok=False,
            error="无归档包，无法全量重建，请运行 sessionpack import --memory-root …")
    # 先全部校验（fail-closed）：任一 BAD 即中止，此时原库与 staging 都未动
    for bundle in archives:
        checked = archive.verify_archive(bundle)
        if not checked.ok:
            codes = ", ".join(issue.code for issue in checked.issues)
            return RebuildReport(
                ok=False,
                error=f"归档 {bundle.name} 校验失败（{codes}），已中止，原索引未改动")

    # staging 名带写者标识（spec9）：并发重建各自建各自的，互不打开、
    # 互不 unlink 对方的 staging；换入步由重试+回滚收敛先后。
    # 保留 .staging 前缀段，既有"无 catalog.sqlite.staging* 残留"断言仍适用。
    staging = layout.catalog.with_name(
        layout.catalog.name
        + f".staging.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        _cleanup(staging)
        if layout.catalog.exists():
            # 原库只读打开确认文件合法后即关闭；此处绝不开写连接
            ro = catalog.connect_readonly(layout.catalog)
            try:
                ro.execute("SELECT COUNT(*) FROM sqlite_master")
            finally:
                ro.close()
            shutil.copyfile(layout.catalog, staging)  # WAL/SHM 不拷
    except OSError:
        # 原库正被他进程持有到连复制都失败：收敛为干净失败而非崩栈。
        _discard_quietly(staging)
        return RebuildReport(
            ok=False,
            error="staging 准备失败（疑似并发重建互踩），原索引未改动")
    conn = catalog.connect(staging)
    try:
        try:
            catalog.init_schema(conn)
        except (CatalogVersionError, CatalogSchemaError):
            # --from-archives 的本职场景就是救门禁拒绝的坏库：复制来的形状
            # 不可用时丢弃复制件，在 staging 上从零建库（原库仍未动）
            conn.close()
            _cleanup(staging)
            conn = catalog.connect(staging)
            catalog.init_schema(conn)
        # spec §5.2 的 fresh 语义：复制来的派生行一律清掉再重放，
        # 否则 upsert_message 的 INSERT OR IGNORE 会跳过旧行、修不掉 FTS 漂移。
        clear_derived_rows(conn)
        report = RebuildReport(ok=True)
        for bundle in archives:
            result = archive.import_archive(conn, bundle)
            report.packages.append({
                "name": bundle.name,
                "archive_id": result.archive_id,
                "sessions": result.sessions,
                "messages": result.messages,
                "messages_inserted": result.messages_inserted,
            })
        drift = reconcile_fts(conn)
        if drift["drift"]:
            raise RebuildError(
                f"重建后自检仍漂移（缺 {drift['fts_missing']}、"
                f"多 {drift['fts_extra']}），原索引未改动")
        stats = catalog.stats(conn)
        report.sessions = stats["sessions"]
        report.messages = stats["messages"]
        report.fts_rows = stats["fts_rows"]
        conn.close()
    except (archive.ArchiveError, sqlite3.Error, RebuildError) as exc:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _cleanup(staging)
        detail = (", ".join(issue.code for issue in exc.issues)
                  if isinstance(exc, archive.ArchiveError) else str(exc))
        return RebuildReport(ok=False, error=f"{detail}；原索引未改动")

    # 换入：原库 → .bak（覆盖旧备份），staging 顶上；失败尽力回滚
    bak = layout.catalog.with_name(layout.catalog.name + ".bak")
    try:
        _cleanup_sidecars_only(layout.catalog)
        if layout.catalog.exists():
            bak.unlink(missing_ok=True)
            _replace_with_retry(layout.catalog, bak)
        _replace_with_retry(staging, layout.catalog)
    except OSError:
        # 回滚：把备份恢复回原位置。恢复自身也可能被占用——此时 bak 是
        # 唯一副本，绝不删除，报清晰错误让用户事后手工归位，不崩栈。
        restored = True
        if bak.exists() and not layout.catalog.exists():
            try:
                _replace_with_retry(bak, layout.catalog)
            except OSError:
                restored = False
        _cleanup(staging)
        if not restored:
            return RebuildReport(
                ok=False,
                error=f"换入失败且备份 {bak.name} 暂被他进程占用、未能归位为 "
                      f"{layout.catalog.name}；数据未丢失，关闭占用进程后将 "
                      f"{bak.name} 手工改名回去即可")
        return RebuildReport(ok=False, error="换入失败，已回滚，原索引未改动")
    _cleanup_sidecars_only(staging)
    report.backup = str(bak) if bak.exists() else None
    return report


def _cleanup_sidecars_only(path: Path) -> None:
    for extra in _sidecars(path):
        extra.unlink(missing_ok=True)
