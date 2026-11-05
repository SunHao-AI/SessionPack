import json
from pathlib import Path

from sessionpack import catalog
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
