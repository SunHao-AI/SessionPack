"""Spec 12 归档删除墓碑：本机隐藏/撤销、墓碑优先传播、rebuild 往返、CLI 冒烟。

断言面收敛在 catalog/read 四处过滤 API 与 run_delete，不测实现细节。
会话 ID 用 24 位十六进制（归档校验 SESSION_ID_RE 的硬要求）。
"""
from __future__ import annotations

import json
import zipfile
from types import SimpleNamespace

from sessionpack import archive, catalog
from sessionpack.cli.write_cmds import run_delete
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord
from sessionpack.paths import ensure_layout
from sessionpack.rebuild import rebuild_from_archives

SID = "0123456789abcdef01234567"
SID2 = "76543210fedcba9876543210"
BODY = "需要隐藏的敏感内容"


def _seed(conn, session_id, *, project_id="p1", body=BODY):
    """建一个带正文的会话（与生产导入同路径：先建项目，再走两个 upsert）。"""
    catalog.upsert_project(conn, ProjectRecord(
        project_id=project_id, display_name=project_id,
        local_path_hint=None, source_kind="session"))
    catalog.upsert_topic(conn, TopicRecord(
        session_id=session_id, project_id=project_id, source_kind="session",
        topic_time="2026-11-08T10:00:00",
        body=f"{session_id} 的主题（关键词：机密词）"))
    catalog.upsert_message(conn, MessageRecord(
        session_id=session_id, message_id=f"{session_id}-m1", seq=1,
        ts="2026-11-08T10:00:01", intent=None, outcome=None,
        actions=(), learned=(), meta=None, body=body,
        project_id=project_id, source_kind="session"))
    conn.commit()


def _open(tmp_path, name):
    """建立数据目录并返回其可写索引连接（run_delete 走同一路径）。"""
    lay = ensure_layout(tmp_path / name)
    conn = catalog.connect(lay.catalog)
    catalog.init_schema(conn)
    return tmp_path / name, conn


def _status(conn, session_id):
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["status"] if row else None


def _delete(root, session_id, *, undo=False, json_out=False):
    """root 是数据目录（Path），run_delete 通过 --data-dir 定位索引。"""
    args = SimpleNamespace(
        session=session_id, undo=undo, json=json_out, data_dir=str(root))
    return run_delete(args)


def test_delete_hides_and_undo_restores(tmp_path):
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    assert _delete(root, SID).exit_code == 0
    assert _status(conn, SID) == "deleted"
    # 用户视图隐藏：详情 / 列表 / 检索（LIKE 与 FTS 两个分支）/ 项目计数
    assert catalog.get_session(conn, SID) is None
    assert [r["session_id"] for r in catalog.list_sessions(conn)] == []
    assert catalog.search_messages(conn, "敏感内容") == []
    assert catalog.search_messages(conn, BODY) == []
    assert catalog.get_projects(conn)[0]["session_count"] == 0
    # 数据本体仍在（隐藏 ≠ 抹除）
    assert len(catalog.get_messages(conn, SID)) == 1
    # undo 恢复
    assert _delete(root, SID, undo=True).exit_code == 0
    assert _status(conn, SID) == "active"
    assert catalog.get_session(conn, SID) is not None
    assert len(catalog.list_sessions(conn)) == 1
    assert catalog.search_messages(conn, "敏感内容")
    assert catalog.get_projects(conn)[0]["session_count"] == 1
    conn.close()


def test_delete_usage_errors(tmp_path):
    # 索引不存在 → 用法错误而非崩溃
    res = _delete(tmp_path / "nope", SID)
    assert res.exit_code == 2 and "索引不存在" in res.err_lines[0]
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    res = _delete(root, "7" * 24)
    assert res.exit_code == 2 and "会话不存在" in res.err_lines[0]
    # 未删除就 undo
    res = _delete(root, SID, undo=True)
    assert res.exit_code == 2 and "无需撤销" in res.err_lines[0]
    # 删除 → undo → 再 undo：第二次 undo 报用法错误
    assert _delete(root, SID).exit_code == 0
    assert _delete(root, SID, undo=True).exit_code == 0
    assert _delete(root, SID, undo=True).exit_code == 2
    # 重复删除幂等成功
    assert _delete(root, SID).exit_code == 0
    assert _delete(root, SID).exit_code == 0
    conn.close()


def test_propagation_old_package_no_revive(tmp_path):
    """B 持旧包数据 → 导入墓碑包隐藏 → 再导入旧包不复活（墓碑优先）。"""
    root_a, a = _open(tmp_path, "a")
    _seed(a, SID)
    out = tmp_path / "out"
    old = archive.build_archive(a, out, "p1")   # 删除前的旧包
    assert _delete(root_a, SID).exit_code == 0  # A 本机删除（写 A 自己的索引）
    tomb = archive.build_archive(a, out, "p1")  # 删除后的墓碑包
    _, b = _open(tmp_path, "b")
    archive.import_archive(b, old.path)
    assert _status(b, SID) == "active"
    archive.import_archive(b, tomb.path)
    assert _status(b, SID) == "deleted"
    assert catalog.get_session(b, SID) is None
    archive.import_archive(b, old.path)         # 更早的旧包 → 不复活
    assert _status(b, SID) == "deleted"
    assert catalog.get_session(b, SID) is None
    b.close()
    a.close()


def test_fresh_device_tombstone_then_old_package(tmp_path):
    """全新设备直接导入墓碑包有效，后续旧包也不复活。"""
    root_a, a = _open(tmp_path, "a")
    _seed(a, SID)
    out = tmp_path / "out"
    old = archive.build_archive(a, out, "p1")
    assert _delete(root_a, SID).exit_code == 0
    tomb = archive.build_archive(a, out, "p1")
    _, fresh = _open(tmp_path, "fresh")
    archive.import_archive(fresh, tomb.path)
    assert _status(fresh, SID) == "deleted"
    assert catalog.list_sessions(fresh) == []
    assert catalog.get_session(fresh, SID) is None
    archive.import_archive(fresh, old.path)
    assert _status(fresh, SID) == "deleted"
    fresh.close()
    a.close()


def test_undo_not_propagated(tmp_path):
    """本机 undo 后导出的 active 包，不会复活他机墓碑。"""
    root_a, a = _open(tmp_path, "a")
    _seed(a, SID)
    out = tmp_path / "out"
    assert _delete(root_a, SID).exit_code == 0
    tomb = archive.build_archive(a, out, "p1")
    _, b = _open(tmp_path, "b")
    archive.import_archive(b, tomb.path)
    assert _status(b, SID) == "deleted"
    assert _delete(root_a, SID, undo=True).exit_code == 0   # A 本机撤销
    assert _status(a, SID) == "active"
    active = archive.build_archive(a, out, "p1")  # undo 后重新出包（active）
    archive.import_archive(b, active.path)
    assert _status(b, SID) == "deleted"           # 墓碑优先：他机仍 deleted
    b.close()
    a.close()


def test_all_tombstone_project_can_export(tmp_path):
    """全墓碑项目仍可归档出包（P7），format_version 仍为 1，verify 通过。"""
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    _seed(conn, SID2)
    assert _delete(root, SID).exit_code == 0
    assert _delete(root, SID2).exit_code == 0
    res = archive.build_archive(conn, tmp_path / "out", "p1")
    assert res.sessions == 2
    check = archive.verify_archive(res.path)
    assert check.ok
    assert check.manifest["format_version"] == 1
    with zipfile.ZipFile(res.path) as zf:
        meta = json.loads(zf.read(f"sessions/{SID}/meta.json"))
    assert meta["status"] == "deleted"
    conn.close()


def test_rebuild_from_archives_keeps_tombstone(tmp_path):
    """delete → export --archive → rebuild --from-archives → 墓碑仍 deleted。"""
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    conn.close()
    assert _delete(root, SID).exit_code == 0
    lay = ensure_layout(root)
    conn = catalog.connect(lay.catalog)
    archive.build_archive(conn, lay.archives, "p1")
    conn.close()
    report = rebuild_from_archives(lay, assume_yes=True)
    assert report.ok, report.error
    conn = catalog.connect(lay.catalog)
    assert _status(conn, SID) == "deleted"
    assert catalog.get_session(conn, SID) is None
    conn.close()


def test_cli_delete_text_and_json(tmp_path, capsys):
    """CLI 端到端冒烟：delete/undo 的文本与 --json 两种输出。"""
    from sessionpack.cli import cmd_delete
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    conn.close()
    args = SimpleNamespace(session=SID, undo=False, json=False,
                           data_dir=str(root))
    assert cmd_delete(args) == 0
    assert "已删除会话" in capsys.readouterr().out
    args.json = True
    assert cmd_delete(args) == 0  # 重复删除幂等成功
    assert json.loads(capsys.readouterr().out) == {
        "session_id": SID, "undo": False, "ok": True}
    args.undo = True
    assert cmd_delete(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "session_id": SID, "undo": True, "ok": True}
    args.json = False
    args.undo = False
    assert cmd_delete(args) == 0
    assert "已删除会话" in capsys.readouterr().out
    args.undo = True
    assert cmd_delete(args) == 0
    assert "已撤销删除" in capsys.readouterr().out
