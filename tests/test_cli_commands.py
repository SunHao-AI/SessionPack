"""run_xxx 直接返回 CommandResult，不必经子进程即可断言文案与退出码。"""

from __future__ import annotations

import argparse
from pathlib import Path

from sessionpack import archive, catalog
from sessionpack.cli import build_parser, main
from sessionpack.cli.output import CommandResult, emit
from sessionpack.cli.read_cmds import run_doctor, run_scan, run_verify
from sessionpack.cli.write_cmds import run_export, run_import, run_pull, run_push


def _ns(**kwargs) -> argparse.Namespace:
    """构造与 parser 等价的 args 对象，避免每个用例重复铺默认值。"""
    base = dict(memory_root=None, data_dir=None, json=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def test_emit_text_lines_split_stdout_and_stderr(capsys) -> None:
    code = emit(CommandResult(2, out_lines=["出"], err_lines=["错"]), as_json=False)
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == "出\n"
    assert captured.err == "错\n"


def test_emit_json_when_data_present(capsys) -> None:
    code = emit(CommandResult(0, out_lines=["不该出现"], data={"a": 1}), as_json=True)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == '{\n  "a": 1\n}\n'
    assert captured.err == ""


def test_emit_ignores_json_flag_when_data_is_none(capsys) -> None:
    """data 为 None 时 as_json 不生效，恒走文本/错误行。"""
    emit(CommandResult(1, err_lines=["只有错误行"]), as_json=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "只有错误行\n"


def test_run_scan_missing_root_is_usage_error(tmp_path: Path) -> None:
    res = run_scan(_ns(memory_root=str(tmp_path / "nope")))
    assert res.exit_code == 2
    assert res.err_lines == [f"记忆目录不存在: {tmp_path / 'nope'}"]
    assert res.out_lines == []
    assert res.data is None


def test_run_doctor_missing_memory_root_text_and_json(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    data_dir = _data_dir(tmp_path)

    res = run_doctor(_ns(memory_root=str(missing), data_dir=str(data_dir)))
    assert res.exit_code == 2
    assert res.data is None
    assert res.out_lines[1] == f"记忆目录：{missing}（不存在）"

    res_json = run_doctor(_ns(memory_root=str(missing), data_dir=str(data_dir),
                              json=True))
    assert res_json.exit_code == 2
    assert res_json.data["memory_root_exists"] is False
    assert res_json.out_lines == []


def test_run_doctor_text_shows_sync_dir(tmp_path: Path) -> None:
    """Spec 6 CFG-3：文本 doctor 显示同步目录状态。"""
    data_dir = _data_dir(tmp_path)
    res = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir)))
    assert any(line == "同步目录：未配置" for line in res.out_lines)

    (data_dir / "config.toml").write_text(
        '[sync]\ndir = "D:\\\\share"\n', encoding="utf-8")
    res2 = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                          data_dir=str(data_dir)))
    assert any("同步目录：D:\\share" == line for line in res2.out_lines)


def test_run_doctor_text_shows_sync_warnings(tmp_path: Path) -> None:
    """Spec 6 CFG-3：config 解析失败时文本 doctor 逐行输出同步警告。"""
    data_dir = _data_dir(tmp_path)
    (data_dir / "config.toml").write_text("[sync\nbroken", encoding="utf-8")
    res = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir)))
    assert any("同步警告：" in line and "解析失败" in line
               for line in res.out_lines)


def test_run_import_missing_memory_root_is_usage_error(tmp_path: Path) -> None:
    res = run_import(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(_data_dir(tmp_path)), archive=None,
                         project=None, since=None, until=None, reparse=False,
                         retry_failed=False, audit=False))
    assert res.exit_code == 2
    assert res.err_lines == [f"记忆目录不存在: {tmp_path / 'nope'}"]


def test_run_export_without_catalog_is_usage_error(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    (data_dir / "catalog.sqlite").write_bytes(b"")
    res = run_export(_ns(data_dir=str(data_dir), out=str(tmp_path / "out"),
                         archive=False, session=None, project=None))
    assert res.exit_code == 2
    assert res.err_lines == ["索引不存在，请先运行 sessionpack import"]


def test_run_verify_marks_bad_archive(tmp_path: Path, monkeypatch) -> None:
    class _Issue:
        def __init__(self, code: str, detail: str) -> None:
            self.code = code
            self.detail = detail

    class _Result:
        def __init__(self, path: Path, ok: bool) -> None:
            self.path = path
            self.ok = ok
            self.manifest = {"archive_id": "a1"} if ok else None
            self.issues = [] if ok else [_Issue("HASH_MISMATCH", "摘要不符")]

    monkeypatch.setattr(archive, "verify_archive",
                        lambda raw: _Result(Path(raw), Path(raw).name == "good.zip"))
    good = tmp_path / "good.zip"
    bad = tmp_path / "bad.zip"
    res = run_verify(_ns(archives=[str(good), str(bad)]))
    assert res.exit_code == 3
    assert res.out_lines == [f"OK  {good}（archive_id a1）",
                             f"BAD {bad}：HASH_MISMATCH"]


def test_run_push_dry_run_and_real(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()

    dry = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=True))
    assert dry.exit_code == 0
    assert dry.out_lines[0].startswith("[dry-run] 将推送 0 个包共 0 字节")

    real = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=False))
    assert real.exit_code == 0
    assert real.out_lines[0].startswith("已推送 0 个包共 0 字节")


def test_run_push_missing_dir_includes_config_diagnosis(tmp_path: Path) -> None:
    """Spec 6 CFG-2：段名拼错时错误输出含诊断，不再只说"未指定"。"""
    data_dir = _data_dir(tmp_path)
    (data_dir / "config.toml").write_text('[snyc]\ndir = "D"\n',
                                          encoding="utf-8")
    res = run_push(_ns(data_dir=str(data_dir), dir=None))
    assert res.exit_code == 2
    assert any("未指定同步目录" in line for line in res.err_lines)
    assert any("[snyc]" in line and "sync" in line for line in res.err_lines)


def test_run_push_config_rejected_still_reports_push(tmp_path: Path) -> None:
    """Spec 6 CFG-1：落配置拒写时，推送成功事实仍报告，且退出码为错误。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    original = "[meta]\nlast_run = 2026-01-01\n"
    (data_dir / "config.toml").write_text(original, encoding="utf-8")

    res = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=False))
    assert res.exit_code == 1
    assert res.out_lines[0].startswith("已推送 0 个包共 0 字节")
    assert any("落配置失败" in line and "[sync]" in line
               for line in res.err_lines)
    assert (data_dir / "config.toml").read_text(encoding="utf-8") == original


def test_run_push_dry_run_never_writes_config(tmp_path: Path) -> None:
    """Spec 6 CFG-1：--dry-run 不触发写路径（锁定现状）。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    original = "[meta]\nlast_run = 2026-01-01\n"
    (data_dir / "config.toml").write_text(original, encoding="utf-8")

    res = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=True))
    assert res.exit_code == 0
    assert (data_dir / "config.toml").read_text(encoding="utf-8") == original


def test_run_pull_requires_existing_dir(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    missing = tmp_path / "sync"

    res = run_pull(_ns(data_dir=str(data_dir), dir=str(missing)))
    assert res.exit_code == 2
    assert res.err_lines == [f"同步目录不存在: {missing}"]

    missing.mkdir()
    ok = run_pull(_ns(data_dir=str(data_dir), dir=str(missing)))
    assert ok.exit_code == 0
    assert ok.out_lines == ["已拉取 0 个包（新增会话 0、消息 0），"
                            "跳过已存在 0，跳过无效包 0"]


def test_run_import_rejects_future_version_db(tmp_path: Path) -> None:
    """Spec 7 D1：库比工具新 → EXIT_ERROR + 带版本号文案，不崩栈。"""
    data_dir = _data_dir(tmp_path)
    conn = catalog.connect(data_dir / "catalog.sqlite")
    catalog.init_schema(conn)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    zip_path = tmp_path / "whatever.zip"
    zip_path.write_bytes(b"")
    res = run_import(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir), archive=str(zip_path),
                         project=None, since=None, until=None, reparse=False,
                         retry_failed=False, audit=False))
    assert res.exit_code == 1
    assert "99" in res.err_lines[0]


def test_run_pull_rejects_legacy_db(tmp_path: Path) -> None:
    """Spec 7 D1：pull 遇缺列史前库 → EXIT_ERROR + 重建导向文案。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    conn = catalog.connect(data_dir / "catalog.sqlite")
    conn.executescript(
        "CREATE TABLE sessions ("
        "session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
        "source_kind TEXT NOT NULL, title TEXT, summary TEXT,"
        "created_at TEXT, updated_at TEXT, status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.commit()
    conn.close()
    res = run_pull(_ns(data_dir=str(data_dir), dir=str(sync_dir)))
    assert res.exit_code == 1
    assert "content_hash" in res.err_lines[0]
    assert "rebuild --from-archives" in res.err_lines[0]


def test_parser_still_builds_every_subcommand() -> None:
    """删除 set_defaults(func=...) 后，参数面必须与拆分前完全一致。"""
    args = build_parser().parse_args(["export", "--all", "--out", "x.zip",
                                      "--archive"])
    assert args.command == "export"
    assert args.all is True and args.out == "x.zip" and args.archive is True


def test_serve_guard_without_catalog_is_usage_error(tmp_path: Path,
                                                    capsys) -> None:
    """serve 特例：不产出 CommandResult，直接返回退出码。"""
    data_dir = _data_dir(tmp_path)
    code = main(["serve", "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == "索引不存在，请先运行 sessionpack import\n"
