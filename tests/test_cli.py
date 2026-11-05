import json
from pathlib import Path

from sessionpack import catalog, cli
from sessionpack.cli import main


def test_doctor_reports_adapter(memory_tree: Path, tmp_path: Path, capsys) -> None:
    code = main(["doctor", "--memory-root", str(memory_tree),
                 "--data-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "trae-cn-memory" in out
    assert "记忆目录" in out
    assert "不读取" in out


def test_scan_json_reports_projects(memory_tree: Path, capsys) -> None:
    code = main(["scan", "--memory-root", str(memory_tree), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["projects"] == 2
    assert data["topic_files"] == 2
    assert data["jsonl_files"] == 2


def test_scan_missing_root_returns_two(tmp_path: Path, capsys) -> None:
    code = main(["scan", "--memory-root", str(tmp_path / "nope")])
    assert code == 2
    assert "不存在" in capsys.readouterr().err


def test_import_creates_catalog(memory_tree: Path, tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    code = main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_dir)])
    assert code == 0
    conn = catalog.connect(data_dir / "catalog.sqlite")
    stats = catalog.stats(conn)
    conn.close()
    assert stats["messages"] == 4
    assert "消息" in capsys.readouterr().out


def test_import_with_audit_passes_and_reports(memory_tree: Path, tmp_path: Path,
                                              capsys) -> None:
    code = main(["import", "--memory-root", str(memory_tree),
                 "--data-dir", str(tmp_path / "data"), "--audit"])
    assert code == 0
    assert "读取边界" in capsys.readouterr().out


def test_export_session_from_cli(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "export"
    code = main(["export", "--data-dir", str(data_dir),
                 "--session", "a" * 24, "--out", str(out)])
    assert code == 0
    assert (out / ("a" * 24 + ".md")).is_file()


def test_export_all_json_from_cli(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "export"
    code = main(["export", "--data-dir", str(data_dir), "--all",
                 "--format", "json", "--out", str(out)])
    assert code == 0
    assert len(list(out.glob("*.json"))) == 2


def test_export_archive_creates_zip(memory_tree: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    out = tmp_path / "archives"
    code = main(["export", "--data-dir", str(data_dir), "--all",
                 "--archive", "--out", str(out)])
    assert code == 0
    assert len(list(out.glob("*.zip"))) == 2


def test_export_unknown_session_returns_two(memory_tree: Path, tmp_path: Path,
                                            capsys) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    code = main(["export", "--data-dir", str(data_dir),
                 "--session", "f" * 24, "--out", str(tmp_path / "out")])
    assert code == 2
    err = capsys.readouterr().err
    assert "会话不存在" in err
    assert "None" not in err


def test_export_unknown_project_returns_two(memory_tree: Path, tmp_path: Path,
                                            capsys) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    code = main(["export", "--data-dir", str(data_dir), "--project", "nope",
                 "--out", str(tmp_path / "out")])
    assert code == 2
    assert "项目不存在或无会话" in capsys.readouterr().err


def test_export_archive_unknown_project_returns_one(memory_tree: Path,
                                                    tmp_path: Path,
                                                    capsys) -> None:
    data_dir = tmp_path / "data"
    main(["import", "--memory-root", str(memory_tree), "--data-dir", str(data_dir)])
    code = main(["export", "--data-dir", str(data_dir), "--archive",
                 "--project", "nope", "--out", str(tmp_path / "archives")])
    assert code == 1
    assert "导出失败" in capsys.readouterr().err


def test_verify_bad_archive_returns_three(tmp_path: Path, capsys) -> None:
    code = main(["verify", str(tmp_path / "nope.zip")])
    assert code == 3
    assert "E_ZIP_UNREADABLE" in capsys.readouterr().out


def test_serve_without_catalog_returns_two(tmp_path: Path, capsys) -> None:
    code = main(["serve", "--data-dir", str(tmp_path / "empty")])
    assert code == 2
    assert "请先运行" in capsys.readouterr().err


def test_export_without_schema_catalog_returns_two(tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "catalog.sqlite").write_bytes(b"")
    code = main(["export", "--data-dir", str(data_dir), "--all",
                 "--out", str(tmp_path / "out")])
    assert code == 2
    captured = capsys.readouterr()
    assert "请先运行" in captured.err
    assert "Traceback" not in captured.err


def test_serve_without_schema_catalog_returns_two(tmp_path: Path, capsys,
                                                 monkeypatch) -> None:
    from sessionpack import server

    calls: list[object] = []
    monkeypatch.setattr(server, "serve", lambda *a, **k: calls.append(a))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "catalog.sqlite").write_bytes(b"")
    code = main(["serve", "--data-dir", str(data_dir)])
    assert code == 2
    assert calls == []
    captured = capsys.readouterr()
    assert "请先运行" in captured.err
    assert "Traceback" not in captured.err


def test_doctor_tolerates_schema_less_catalog(memory_tree: Path, tmp_path: Path,
                                             capsys) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "catalog.sqlite").write_bytes(b"")
    code = main(["doctor", "--memory-root", str(memory_tree),
                 "--data-dir", str(data_dir)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "索引未初始化" in captured.out


def test_main_falls_back_on_unexpected_error(memory_tree: Path, tmp_path: Path,
                                             monkeypatch, capsys) -> None:
    def boom(args):
        raise RuntimeError("意外崩溃")

    monkeypatch.setattr(cli, "_cmd_scan", boom)
    code = main(["scan", "--memory-root", str(memory_tree)])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("错误: ")
    assert "Traceback" not in captured.err


def test_zero_byte_catalog_not_rewritten_by_precheck(
    memory_tree: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    catalog_file = data_dir / "catalog.sqlite"
    catalog_file.write_bytes(b"")
    code = main(["export", "--session", "a" * 24, "--out", str(tmp_path / "out"),
                 "--data-dir", str(data_dir)])
    assert code == 2
    assert catalog_file.stat().st_size == 0


def test_non_sqlite_catalog_is_not_rewritten(
    memory_tree: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    catalog_file = data_dir / "catalog.sqlite"
    catalog_file.write_bytes(b"definitely not a database")
    code = main(["export", "--session", "a" * 24, "--out", str(tmp_path / "out"),
                 "--data-dir", str(data_dir)])
    assert code == 2
    assert catalog_file.read_bytes() == b"definitely not a database"
