"""failure_text：OSError 摘要 + 路径脱敏 + 截断。"""
from __future__ import annotations

from sessionpack.textutil import failure_text


def test_oserror_uses_strerror_and_filename_hint():
    exc = OSError(2, "No such file or directory")
    exc.filename = r"C:\Users\me\.trae\data.jsonl"
    text = failure_text(exc)
    assert text == "No such file or directory（文件位于数据目录内）"


def test_path_redaction_and_limit():
    exc = ValueError(r"bad entry at C:\Users\me\a.jsonl tail")
    text = failure_text(exc, limit=200)
    assert "<路径>" in text
    assert "C:\\Users" not in text
    assert len(failure_text(ValueError("x" * 300))) <= 60
