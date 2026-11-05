import json
from pathlib import Path

import pytest

from sessionpack import catalog, export, summary
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


@pytest.fixture
def filled_db(memory_tree: Path, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    return db


def _session(conn, session_id: str = "a" * 24):
    return catalog.get_session(conn, session_id), catalog.get_messages(conn, session_id)


def test_summary_is_deterministic_and_has_sections(filled_db: Path) -> None:
    conn = catalog.connect(filled_db)
    session, messages = _session(conn)
    first = summary.render_markdown(session, messages)
    second = summary.render_markdown(session, messages)
    assert first == second
    assert "## 关键结论" in first
    assert "## 涉及文件" in first
    assert "批量提交比逐文件提交快" in first
    assert str(filled_db) not in first


def test_summary_dedupes_learned_and_sorts_files() -> None:
    session = {
        "session_id": "a" * 24,
        "project_id": "-d-Work-RepoA",
        "display_name": "-d-Work-RepoA",
        "title": "测试会话",
        "summary": "测试摘要",
        "created_at": "2026-01-01 10:00:00",
        "updated_at": "2026-01-01 10:10:00",
    }
    messages = [
        {"session_id": "a" * 24, "message_id": "m1", "seq": 1,
         "ts": "2026-01-01 10:00:00", "intent": "改界面", "outcome": "完成",
         "actions": "改 app.js|改 app.js", "learned": "结论甲|结论乙", "body": ""},
        {"session_id": "a" * 24, "message_id": "m2", "seq": 2,
         "ts": "2026-01-01 10:10:00", "intent": "补测试", "outcome": "通过",
         "actions": "改 src/a.py", "learned": "结论甲", "body": ""},
    ]
    data = summary.build_summary(session, messages)
    assert data["learned"] == ["结论甲", "结论乙"]
    assert data["actions"] == ["改 app.js", "改 src/a.py"]
    assert data["files"] == ["app.js", "src/a.py"]
    assert data["latest_outcome"] == "通过"


def test_extract_file_mentions_strips_absolute_roots() -> None:
    texts = [
        r"改 E:\Workplace\SessionPack\sessionpack\summary.py",
        "改 /home/dev/repo/main.py",
        r"改 D:\repo\src\a.py 和 ./tools/b.py",
    ]
    assert summary.extract_file_mentions(texts) == [
        "a.py",
        "main.py",
        "summary.py",
        "tools/b.py",
    ]


def test_extract_file_mentions_strips_dotdot() -> None:
    found = summary.extract_file_mentions(["见 ../src/utils.py 与 ../../pkg/app.js"])
    assert "src/utils.py" in found
    assert "pkg/app.js" in found
    assert not any(item.startswith("..") for item in found)


def test_export_session_writes_markdown_and_json(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    out = tmp_path / "out"
    md, _ = export.export_session(conn, "a" * 24, out, "markdown")
    js, _ = export.export_session(conn, "a" * 24, out, "json")
    assert md.name == "a" * 24 + ".md"
    assert "实现归档包的校验流程" in md.read_text(encoding="utf-8")
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["summary"]["session_id"] == "a" * 24
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["actions"] == ["读取 topics.md"]
    assert list(out.glob("*.tmp")) == []


def test_export_all_covers_every_session(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    result = export.export_all(conn, tmp_path / "all")
    assert result.sessions == 2
    assert result.messages == 4
    assert len(result.paths) == 2
    assert all(path.is_file() for path in result.paths)


def test_export_unknown_session_raises(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    with pytest.raises(KeyError):
        export.export_session(conn, "f" * 24, tmp_path / "out")


def test_export_rejects_illegal_session_id(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    with pytest.raises(ValueError):
        export.export_session(conn, "../../escape", tmp_path / "out")


def test_export_session_returns_path_and_count(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    path, count = export.export_session(conn, "a" * 24, tmp_path / "out", "json")
    assert path.name == "a" * 24 + ".json"
    assert count == 2


def test_export_all_no_limit(filled_db: Path, tmp_path: Path) -> None:
    conn = catalog.connect(filled_db)
    result = export.export_all(conn, tmp_path / "all")
    assert result.sessions == catalog.stats(conn)["sessions"]


def test_build_summary_dedupes_learned(filled_db: Path) -> None:
    conn = catalog.connect(filled_db)
    session = catalog.get_session(conn, "a" * 24)
    messages = catalog.get_messages(conn, "a" * 24)
    data = summary.build_summary(session, messages)
    learned = data["learned"]
    assert len(learned) == len(set(learned))
