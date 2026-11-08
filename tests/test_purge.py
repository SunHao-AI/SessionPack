"""Spec 13 物理清除：终态抹除、三道不复活卡、重打包、CLI 冒烟、同步报告。

会话 ID 用 24 位十六进制（归档校验 SESSION_ID_RE 的硬要求）。
_seed/_open/_status 与 tests/test_delete_tombstone.py 同构（不跨测试文件导入，
避免测试互相依赖）。
"""
from __future__ import annotations

import json
import zipfile
from types import SimpleNamespace

import pytest

from sessionpack import archive, catalog
from sessionpack.archive.repak import RepakError, repak_excluding_session
from sessionpack.catalog.write import (
    purge_session,
    refresh_session_hashes,
    upsert_message,
    upsert_topic,
)
from sessionpack.cli import build_parser, main
from sessionpack.cli.write_cmds import run_delete
from sessionpack.model import MessageRecord, ProjectRecord, TopicRecord
from sessionpack.paths import ensure_layout
from sessionpack.rebuild import rebuild_from_archives
from sessionpack.sync import find_archives_with_session

SID = "0123456789abcdef01234567"
SID2 = "76543210fedcba9876543210"
BODY = "需要彻底抹除的敏感内容"


def _seed(conn, session_id, *, project_id="p1", body=BODY):
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
    lay = ensure_layout(tmp_path / name)
    conn = catalog.connect(lay.catalog)
    catalog.init_schema(conn)
    return tmp_path / name, conn


def _status(conn, session_id):
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["status"] if row else None


def _purge_setup(tmp_path, name="root"):
    """建库、播种、delete 到 deleted 态，返回 (root, conn)。"""
    root, conn = _open(tmp_path, name)
    _seed(conn, SID)
    assert catalog.mark_session_deleted(
        conn, SID, now="2026-11-08T11:00:00")
    conn.commit()
    return root, conn


def test_purge_erases_content_and_marks_terminal(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is True
    conn.commit()
    row = conn.execute(
        "SELECT status, title, summary, content_hash, updated_at "
        "FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["status"] == "purged"
    assert row["title"] is None and row["summary"] is None
    assert row["content_hash"] is None
    assert row["updated_at"] == "2026-11-08T12:00:00"
    # sessions 行保留（purge 卡载体）；消息与 FTS 物理删除
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 0


def test_purge_hides_everything(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00")
    conn.commit()
    assert catalog.get_session(conn, SID) is None
    assert catalog.list_sessions(conn) == []
    assert catalog.search_messages(conn, "敏感") == []
    assert catalog.search_messages(conn, BODY) == []
    assert catalog.get_messages(conn, SID) == []
    assert catalog.get_projects(conn)[0]["session_count"] == 0
    # 原始状态查询仍可达（undo/purge 判家）
    assert catalog.get_session_status(conn, SID) == "purged"


def test_purge_only_from_deleted(tmp_path):
    root, conn = _open(tmp_path, "root")
    _seed(conn, SID)
    # active 态直接 purge：拒绝且不抹数据
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is False
    assert _status(conn, SID) == "active"
    assert len(catalog.get_messages(conn, SID)) == 1
    # deleted → purged → 再 purge：幂等 False，updated_at 不变
    catalog.mark_session_deleted(conn, SID, now="2026-11-08T11:00:00")
    conn.commit()
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is True
    conn.commit()
    assert purge_session(conn, SID, now="2026-11-08T13:00:00") is False
    row = conn.execute(
        "SELECT updated_at FROM sessions WHERE session_id=?", (SID,)).fetchone()
    assert row["updated_at"] == "2026-11-08T12:00:00"
    # 防降级：purged 后 delete 不再改写状态
    assert catalog.mark_session_deleted(conn, SID, now="2026-11-08T14:00:00") is False
    assert _status(conn, SID) == "purged"
    conn.close()


def test_no_revive_via_upsert(tmp_path):
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00")
    conn.commit()
    # 守卫①：purged 会话上的主题/消息写入全部 no-op
    topic_result = upsert_topic(conn, TopicRecord(
        session_id=SID, project_id="p1", source_kind="session",
        topic_time="2026-11-09T10:00:00", body="迟到的主题"))
    assert topic_result.inserted is False and topic_result.session_created is False
    msg_result = upsert_message(conn, MessageRecord(
        session_id=SID, message_id=f"{SID}-m2", seq=2,
        ts="2026-11-09T10:00:01", intent=None, outcome=None,
        actions=(), learned=(), meta=None, body="迟到的正文",
        project_id="p1", source_kind="session"))
    assert msg_result.inserted is False and msg_result.session_created is False
    conn.commit()
    assert _status(conn, SID) == "purged"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    conn.close()


def test_import_purged_package_wipes_local_copy(tmp_path):
    """B 已导入旧包 → 导入 purge 卡包收卡抹除 → 再导旧包不复活。"""
    _, a = _purge_setup(tmp_path, "a")
    out = tmp_path / "out"
    old = archive.build_archive(a, out, "p1")        # purge 前的包（含正文）
    assert catalog.purge_session(a, SID, now="2026-11-08T12:00:00")
    a.commit()
    card = archive.build_archive(a, out, "p1")       # purge 卡包（空正文）
    _, b = _open(tmp_path, "b")
    archive.import_archive(b, old.path)
    assert catalog.get_session_status(b, SID) == "deleted"
    archive.import_archive(b, card.path)
    assert catalog.get_session_status(b, SID) == "purged"
    assert catalog.get_messages(b, SID) == []
    assert catalog.search_messages(b, "敏感") == []
    # content_hash 保持 NULL：收尾 refresh_session_hashes 不得回写 purge 卡
    assert b.execute("SELECT content_hash FROM sessions WHERE session_id=?",
                     (SID,)).fetchone()[0] is None
    archive.import_archive(b, old.path)              # 旧包回灌 → 守卫①拦截
    assert catalog.get_session_status(b, SID) == "purged"
    assert catalog.get_messages(b, SID) == []
    b.close()
    a.close()


def test_active_local_copy_forced_purged_on_import(tmp_path):
    """本机 active 态收到 purge 卡：强制抹除升级（purge 传播强于墓碑）。"""
    _, a = _purge_setup(tmp_path, "a")
    assert catalog.purge_session(a, SID, now="2026-11-08T12:00:00")
    a.commit()
    card = archive.build_archive(a, tmp_path / "out", "p1")
    _, b = _open(tmp_path, "b")
    _seed(b, SID)                                    # B 本地 active 且有正文
    archive.import_archive(b, card.path)
    assert catalog.get_session_status(b, SID) == "purged"
    assert catalog.get_messages(b, SID) == []
    b.close()
    a.close()


def test_fresh_device_import_purged_package(tmp_path):
    """全新设备直接导入 purge 卡包：落 purged 终态卡、无任何内容。"""
    _, a = _purge_setup(tmp_path, "a")
    assert catalog.purge_session(a, SID, now="2026-11-08T12:00:00")
    a.commit()
    card = archive.build_archive(a, tmp_path / "out", "p1")
    _, fresh = _open(tmp_path, "fresh")
    archive.import_archive(fresh, card.path)
    assert catalog.get_session_status(fresh, SID) == "purged"
    assert catalog.list_sessions(fresh) == []
    assert catalog.get_messages(fresh, SID) == []
    fresh.close()
    a.close()


def _two_session_package(tmp_path):
    """一个包两个会话：SID（将被剔除）与 SID2（保留），SID 已 delete。"""
    _, conn = _open(tmp_path, "a")
    _seed(conn, SID)
    _seed(conn, SID2, body="保留的正文")
    catalog.mark_session_deleted(conn, SID, now="2026-11-08T11:00:00")
    conn.commit()
    item = archive.build_archive(conn, tmp_path / "out", "p1")
    conn.close()
    return item.path


def test_repak_replaces_session_with_purge_card(tmp_path):
    src = _two_session_package(tmp_path)
    new = repak_excluding_session(src, SID, tmp_path / "out")
    check = archive.verify_archive(new)
    assert check.ok
    with zipfile.ZipFile(new) as zf:
        card = json.loads(zf.read(f"sessions/{SID}/meta.json"))
        assert card["status"] == "purged"
        assert set(card) == {"session_id", "project_id", "source_kind",
                             "status", "created_at", "updated_at"}
        # 空 ndjson 裁决：条目保留、内容为 0 字节（配对不变量不允许删条目）
        assert zf.read(f"sessions/{SID}/messages.ndjson") == b""
        kept = json.loads(zf.read(f"sessions/{SID2}/meta.json"))
        assert kept["status"] == "active"
        body = zf.read(f"sessions/{SID2}/messages.ndjson").decode("utf-8")
        assert "保留的正文" in body
        manifest = json.loads(zf.read("manifest.json"))
    # 该会话正文抹除后：会话数不变（卡片保留）、消息数减 1
    assert manifest["counts"] == {"sessions": 2, "messages": 1}
    assert new.name != src.name          # archive_id 变 → 本机改名（ADR-007 豁免）
    assert src.exists()                  # 旧包删除由调用方负责


def test_repak_rejects_missing_session(tmp_path):
    src = _two_session_package(tmp_path)
    with pytest.raises(RepakError):
        repak_excluding_session(src, "f" * 24, tmp_path / "out")


def test_repak_rejects_invalid_package(tmp_path):
    src = _two_session_package(tmp_path)
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(broken, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr("extra.txt", "x")  # manifest 外多余条目 → E_UNREGISTERED_FILE
    with pytest.raises(archive.ArchiveError):
        repak_excluding_session(broken, SID, tmp_path / "out")


def test_find_archives_with_session_scan(tmp_path):
    src = _two_session_package(tmp_path)          # 位于 tmp_path/out
    dest = tmp_path / "sync"
    dest.mkdir()
    (dest / src.name).write_bytes(src.read_bytes())
    (dest / ".tmp-half.zip").write_bytes(b"PK")   # 临时前缀必须被跳过
    assert find_archives_with_session(dest, SID) == [src.name]
    assert find_archives_with_session(dest, SID2) == [src.name]
    assert find_archives_with_session(dest, "f" * 24) == []
    assert find_archives_with_session(tmp_path / "none", SID) == []


def _purge_cli(root, session_id, *, yes=True, json_out=False, directory=None):
    args = SimpleNamespace(session=session_id, undo=False, purge=True,
                           yes=yes, json=json_out, data_dir=str(root),
                           dir=directory)
    return run_delete(args)


def _purge_seeded(tmp_path, name="root"):
    """建库播种 → CLI delete → 出包到 lay.archives，返回 (root, lay)。"""
    root, conn = _purge_setup(tmp_path, name)
    lay = ensure_layout(root)
    archive.build_archive(conn, lay.archives, "p1")
    conn.close()
    return root, lay


def test_cli_purge_gates(tmp_path):
    root, lay = _purge_seeded(tmp_path)
    conn = catalog.connect(lay.catalog)
    # --purge 缺 --yes → 用法错误，数据未动
    assert _purge_cli(root, SID, yes=False).exit_code == 2
    assert catalog.get_session_status(conn, SID) == "deleted"
    # --undo 与 --purge 互斥
    args = SimpleNamespace(session=SID, undo=True, purge=True, yes=True,
                           json=False, data_dir=str(root), dir=None)
    assert run_delete(args).exit_code == 2
    conn.close()
    # active 态直接 purge → 用法错误（G2 门槛）
    root2, conn2 = _open(tmp_path, "b")
    _seed(conn2, SID2)
    conn2.close()
    res = _purge_cli(root2, SID2)
    assert res.exit_code == 2 and "先" in res.err_lines[0]


def test_cli_purge_erases_and_repacks(tmp_path):
    root, lay = _purge_seeded(tmp_path)
    old_name = lay.archives.glob("*.zip").__next__().name
    res = _purge_cli(root, SID, json_out=True)
    assert res.exit_code == 0
    assert res.data == {"session_id": SID, "purged": True,
                        "messages_removed": 1, "archives_repacked": 1,
                        "archives_failed": [], "sync_stale": []}
    zips = list(lay.archives.glob("*.zip"))
    assert len(zips) == 1 and zips[0].name != old_name   # 旧包已删、新包在场
    assert archive.verify_archive(zips[0]).ok
    conn = catalog.connect(lay.catalog)
    assert catalog.get_session_status(conn, SID) == "purged"
    assert catalog.get_messages(conn, SID) == []
    conn.close()


def test_cli_purge_idempotent(tmp_path):
    root, lay = _purge_seeded(tmp_path)
    assert _purge_cli(root, SID).exit_code == 0
    res = _purge_cli(root, SID, json_out=True)          # 已是 purged：幂等成功
    assert res.exit_code == 0
    assert res.data["purged"] is True and res.data["archives_repacked"] == 0


def test_cli_purge_end_to_end_via_parser(tmp_path, capsys):
    """真实 CLI 路径：build_parser 必须给出 args.dir，main 走通 purge 全链。"""
    root, lay = _purge_seeded(tmp_path)
    args = build_parser().parse_args(
        ["delete", "--session", SID, "--purge", "--yes"])
    assert args.dir is None                             # delete 也带 --dir（审查修复）
    code = main(["delete", "--session", SID, "--purge", "--yes",
                 "--data-dir", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"session_id": SID, "purged": True,
                       "messages_removed": 1, "archives_repacked": 1,
                       "archives_failed": [], "sync_stale": []}
    conn = catalog.connect(lay.catalog)
    assert catalog.get_session_status(conn, SID) == "purged"
    conn.close()


def test_rebuild_keeps_purge_card(tmp_path):
    """purge → rebuild --from-archives → 终态卡仍在、正文不回。"""
    root, lay = _purge_seeded(tmp_path)
    assert _purge_cli(root, SID).exit_code == 0
    report = rebuild_from_archives(lay, assume_yes=True)
    assert report.ok, report.error
    conn = catalog.connect(lay.catalog)
    assert catalog.get_session_status(conn, SID) == "purged"
    assert catalog.get_session(conn, SID) is None
    assert catalog.get_messages(conn, SID) == []
    conn.close()


def test_cli_purge_reports_stale_sync_dir(tmp_path):
    """同步目录含旧包 → 只报告不改动，命令仍成功。"""
    root, lay = _purge_seeded(tmp_path)
    stale = tmp_path / "sync"
    stale.mkdir()
    names = sorted(p.name for p in lay.archives.glob("*.zip"))
    for item in lay.archives.glob("*.zip"):
        (stale / item.name).write_bytes(item.read_bytes())
    res = _purge_cli(root, SID, json_out=True, directory=str(stale))
    assert res.exit_code == 0
    assert res.data["sync_stale"] == names
    assert res.data["archives_repacked"] == 1
    # 同步目录内容原样未动
    assert sorted(p.name for p in stale.glob("*.zip")) == names


def test_repak_rejects_manifest_without_project_section(tmp_path):
    """手造缺 project 节的包（files 未动故仍能过 verify）→ RepakError 而非 KeyError。"""
    src = _two_session_package(tmp_path)
    hollow = tmp_path / "hollow.zip"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(hollow, "w") as zout:
        for info in zin.infolist():
            payload = zin.read(info.filename)
            if info.filename == "manifest.json":
                payload = json.dumps(
                    {k: v for k, v in json.loads(payload).items()
                     if k != "project"}).encode("utf-8")
            zout.writestr(info, payload)
    with pytest.raises(RepakError):
        repak_excluding_session(hollow, SID, tmp_path / "out")


def test_find_archives_with_session_skips_non_dict_meta(tmp_path):
    """同步目录里 meta.json 根非 dict 的包：跳过、不抛异常、不阻塞扫描。"""
    src = _two_session_package(tmp_path)
    dest = tmp_path / "sync"
    dest.mkdir()
    (dest / src.name).write_bytes(src.read_bytes())
    junk = dest / "junk.zip"
    with zipfile.ZipFile(junk, "w") as zout:
        zout.writestr(f"sessions/{SID}/meta.json", json.dumps([SID]).encode())
    assert find_archives_with_session(dest, SID) == [src.name]


# ---------------------------------------------------------------------------
# F1 回归：refresh_session_hashes 仅回写 active 行，purged 行保持 NULL
# ---------------------------------------------------------------------------

def test_refresh_session_hashes_preserves_purged_content_hash_null(tmp_path):
    """purged 行直接调 refresh_session_hashes 后 content_hash 仍为 None。"""
    _, conn = _purge_setup(tmp_path)
    assert purge_session(conn, SID, now="2026-11-08T12:00:00") is True
    conn.commit()
    # 直接调用：touched 含有 purged 会话（模拟 importer 无条件 touched）
    refresh_session_hashes(conn, {SID})
    conn.commit()
    row = conn.execute(
        "SELECT content_hash FROM sessions WHERE session_id=?", (SID,)
    ).fetchone()
    assert row["content_hash"] is None
    conn.close()


def test_old_package_install_preserves_purged_status_and_hash(tmp_path):
    """purge 后用旧包（meta 无 purged 标记）回灌：status 仍 purged、hash 仍 None。"""
    _, a = _purge_setup(tmp_path, "a")
    # 在 purge 前导出"旧包"（含正文，meta 无 status=purged）
    old = archive.build_archive(a, tmp_path / "out-old", "p1")
    assert catalog.purge_session(a, SID, now="2026-11-08T12:00:00")
    a.commit()
    # 用 purge 卡包把 b 初始化为 purged 态
    card = archive.build_archive(a, tmp_path / "out-card", "p1")
    _, b = _open(tmp_path, "b")
    archive.import_archive(b, card.path)
    assert catalog.get_session_status(b, SID) == "purged"
    # 旧包回灌：守卫①拦截 upsert；touched 含 SID → refresh 不得写 purged 行
    archive.import_archive(b, old.path)
    assert catalog.get_session_status(b, SID) == "purged"
    assert b.execute(
        "SELECT content_hash FROM sessions WHERE session_id=?", (SID,)
    ).fetchone()[0] is None
    b.close()
    a.close()
