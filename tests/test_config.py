from pathlib import Path

from sessionpack import config


def test_missing_config_returns_empty(tmp_path: Path) -> None:
    data, warnings = config.load_config(tmp_path)
    assert data == {}
    assert warnings == []
    assert config.sync_dir(tmp_path) is None


def test_set_and_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "share" / "打包 目录"
    config.set_sync_dir(tmp_path, target)
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["sync"]["dir"] == str(target)
    assert config.sync_dir(tmp_path) == target


def test_corrupt_config_treated_as_empty_with_warning(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("not = = toml", encoding="utf-8")
    data, warnings = config.load_config(tmp_path)
    assert data == {}
    assert warnings and "config.toml" in warnings[0]
    assert config.sync_dir(tmp_path) is None


def test_config_without_sync_section(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[other]\nx = 1\n", encoding="utf-8")
    assert config.sync_dir(tmp_path) is None
