"""P2 门面：SourceFile.session_id 由适配器填充；importer 不再触碰适配器私有符号。"""
from __future__ import annotations

from pathlib import Path

from sessionpack.adapters import get_adapter


def test_enumerate_fills_session_id(tmp_path):
    sid = "a" * 24
    day = tmp_path / "projects" / "-e-demo" / "20260101"
    day.mkdir(parents=True)
    (day / f"session_memory_{sid}.jsonl").write_text(
        '{"role":"user","content":"x","ts":"2026-01-01 00:00:00"}\n',
        encoding="utf-8")
    refs = list(get_adapter().enumerate(tmp_path))
    msgs = [r for r in refs if r.session_id]
    assert len(msgs) == 1
    assert msgs[0].session_id == sid


def test_adapter_display_name():
    adapter = get_adapter()
    assert adapter.display_name("-e-Workplace-Foo") == "E:\\Workplace\\Foo"


def test_importer_has_no_adapter_internals_import():
    src = (Path(__file__).resolve().parents[1]
           / "sessionpack" / "importer.py").read_text(encoding="utf-8")
    assert "from sessionpack.adapters.trae_memory import" not in src
    assert "project_display_name" not in src
    assert "SESSION_FILE_RE" not in src
