from pathlib import Path

import pytest

from sessionpack.audit import ReadAudit, ReadBoundaryError
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def test_audit_records_only_declared_files(memory_tree: Path, tmp_path: Path) -> None:
    adapter = TraeMemoryAdapter()
    declared = adapter.declared_paths(memory_tree)
    with ReadAudit(memory_tree, declared) as audit:
        import_source(adapter, memory_tree, tmp_path / "catalog.sqlite")
    audit.check(declared)
    assert len(audit.opened) == 4


def test_audit_flags_undeclared_open(memory_tree: Path) -> None:
    token = memory_tree / "trae-jwt-token"
    with ReadAudit(memory_tree, []) as audit:
        token.read_text(encoding="utf-8")
    with pytest.raises(ReadBoundaryError) as exc:
        audit.check([])
    assert "trae-jwt-token" in str(exc.value)


def test_violations_deduplicated(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    evil = root / "evil.txt"
    evil.write_text("x", encoding="utf-8")
    with ReadAudit(root, []) as audit:
        evil.read_text(encoding="utf-8")
        evil.read_text(encoding="utf-8")
    assert audit.violations == ["evil.txt"]


def test_hook_inactive_after_exit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    evil = root / "evil.txt"
    evil.write_text("x", encoding="utf-8")
    with ReadAudit(root, []) as audit:
        pass
    assert audit.active is False
    evil.read_text(encoding="utf-8")
    assert audit.violations == []
