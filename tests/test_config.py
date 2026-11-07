from pathlib import Path

import pytest

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


def test_set_sync_dir_non_bmp_path_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "分享\U0001F600目录"
    config.set_sync_dir(tmp_path, target)
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["sync"]["dir"] == str(target)


def test_set_sync_dir_preserves_other_sections(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[other]\nname = "保留"\ncount = 3\nflag = true\n', encoding="utf-8")
    target = tmp_path / "share"
    config.set_sync_dir(tmp_path, target)
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["sync"]["dir"] == str(target)
    assert data["other"] == {"name": "保留", "count": 3, "flag": True}
    assert not (tmp_path / "config.toml.tmp").exists()


def test_set_sync_dir_updates_in_place(tmp_path: Path) -> None:
    first = tmp_path / "share-a"
    second = tmp_path / "share-b"
    config.set_sync_dir(tmp_path, first)
    config.set_sync_dir(tmp_path, second)
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["sync"]["dir"] == str(second)


# ---- Spec 6 CFG-1：写回保真护栏 ----

def _keep(tmp_path: Path, text: str) -> str:
    """摆一个手写 config.toml 并返回原文（用于断言分毫未动）。"""
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    return text


def test_set_sync_dir_rejects_nested_section(tmp_path: Path) -> None:
    original = _keep(tmp_path, '[sync]\ndir = "x"\n[sync.auth]\ntoken = "t"\n')
    with pytest.raises(config.ConfigWriteError) as exc:
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert "sync.auth" in str(exc.value)
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_set_sync_dir_rejects_date_value(tmp_path: Path) -> None:
    original = _keep(tmp_path, "[meta]\nlast_run = 2026-01-01\n")
    with pytest.raises(config.ConfigWriteError) as exc:
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert "meta.last_run" in str(exc.value)
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_set_sync_dir_rejects_quoted_section_name(tmp_path: Path) -> None:
    original = _keep(tmp_path, '["my key"]\nx = 1\n')
    with pytest.raises(config.ConfigWriteError) as exc:
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert "my key" in str(exc.value)
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_set_sync_dir_rejects_quoted_key_in_section(tmp_path: Path) -> None:
    original = _keep(tmp_path, '[other]\n"my key" = 1\n')
    with pytest.raises(config.ConfigWriteError) as exc:
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert "other.my key" in str(exc.value)
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_set_sync_dir_rejects_corrupt_file_keeps_bytes(tmp_path: Path) -> None:
    original = _keep(tmp_path, '[other]\nkeep = "please"\n[sync]\ndir = "D')
    before = (tmp_path / "config.toml").read_bytes()
    with pytest.raises(config.ConfigWriteError):
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert (tmp_path / "config.toml").read_bytes() == before
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_set_sync_dir_rejection_leaves_no_tmp(tmp_path: Path) -> None:
    _keep(tmp_path, '[[repos]]\nname = "x"\n')
    with pytest.raises(config.ConfigWriteError):
        config.set_sync_dir(tmp_path, tmp_path / "share")
    assert not (tmp_path / "config.toml.tmp").exists()


def test_set_sync_dir_allows_scalars_and_empty_section(tmp_path: Path) -> None:
    """放行面：顶层标量、空段、扁平段混合的既有文件必须继续可写。"""
    _keep(tmp_path, 'verbose = true\n[empty]\n[other]\nname = "k"\nn = 2\n')
    target = tmp_path / "share"
    config.set_sync_dir(tmp_path, target)
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["sync"]["dir"] == str(target)
    assert data["verbose"] is True
    assert data["empty"] == {}
    assert data["other"] == {"name": "k", "n": 2}


def test_set_sync_dir_repairs_scalar_sync(tmp_path: Path) -> None:
    """sync 原本是标量：按"修复"处理，正常写出不报错且往返保真。"""
    _keep(tmp_path, 'sync = "D"\n[other]\nx = 1\n')
    target = tmp_path / "share"
    config.set_sync_dir(tmp_path, target)
    assert config.sync_dir(tmp_path) == target
    data, warnings = config.load_config(tmp_path)
    assert warnings == []
    assert data["other"] == {"x": 1}


# ---- Spec 6 CFG-2：形状诊断 ----

def test_shape_warning_near_section_name(tmp_path: Path) -> None:
    _keep(tmp_path, '[snyc]\ndir = "D"\n')
    data, warnings = config.load_config(tmp_path)
    assert config.sync_dir(tmp_path) is None
    assert any("[snyc]" in w and "sync" in w for w in warnings)


def test_shape_warning_dir_wrong_type(tmp_path: Path) -> None:
    _keep(tmp_path, "[sync]\ndir = 123\n")
    _, warnings = config.load_config(tmp_path)
    assert any("dir" in w and "int" in w for w in warnings)


def test_shape_warning_empty_dir(tmp_path: Path) -> None:
    _keep(tmp_path, '[sync]\ndir = ""\n')
    _, warnings = config.load_config(tmp_path)
    assert any("为空" in w for w in warnings)


def test_shape_no_warning_for_normal_missing_and_other(tmp_path: Path) -> None:
    """防噪音回归：正常配置、无配置、无关段名均零警告。"""
    assert config.load_config(tmp_path)[1] == []          # 无文件
    _keep(tmp_path, '[sync]\ndir = "D"\n')                # 正常
    assert config.load_config(tmp_path)[1] == []
    _keep(tmp_path, '[other]\nx = 1\n')                   # 无关段不误报
    assert config.load_config(tmp_path)[1] == []
