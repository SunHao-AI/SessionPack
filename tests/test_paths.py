from pathlib import Path

from sessionpack.paths import default_memory_root, ensure_layout, resolve_data_dir


def test_ensure_layout_creates_subdirs(tmp_path: Path) -> None:
    layout = ensure_layout(tmp_path)
    assert layout.catalog == tmp_path / "catalog.sqlite"
    assert layout.archives.is_dir()
    assert layout.objects.is_dir()
    assert layout.temp.is_dir()
    assert not layout.catalog.exists()


def test_resolve_data_dir_prefers_argument(tmp_path: Path) -> None:
    assert resolve_data_dir(str(tmp_path)) == tmp_path.resolve()


def test_default_memory_root_points_to_trae_cn() -> None:
    assert default_memory_root().name == "memory"
    assert default_memory_root().parent.name == ".trae-cn"
