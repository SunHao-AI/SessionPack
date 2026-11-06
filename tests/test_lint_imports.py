"""lint_imports 自测：三类规则各命中 + 合规源码 0 命中 + 包内边/相对导入豁免。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lint_imports  # noqa: E402


def test_layer_rule_hits_l3_importing_l4():
    src = "from sessionpack import server\n"
    out = lint_imports.check_source(src, "sessionpack/importer.py")
    assert len(out) == 1
    assert "E_LAYER" in out[0]
    assert out[0].startswith("sessionpack/importer.py:1: E_LAYER")


def test_layer_rule_ok_l3_importing_l2():
    src = "from sessionpack import catalog\n"
    assert lint_imports.check_source(src, "sessionpack/importer.py") == []


def test_private_rule_hits_cross_module_underscore_call():
    src = "from sessionpack import archive\nx = archive._failure_text(e)\n"
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    priv = [o for o in out if "E_PRIVATE" in o]
    assert len(priv) == 1
    assert priv[0].startswith("sessionpack/sync.py:2: E_PRIVATE")


def test_private_rule_ok_self_and_os():
    src = "import os\nos._exit(9)\nself._x()\n"
    assert lint_imports.check_source(src, "sessionpack/importer.py") == []


def test_sql_rule_hits_write_on_managed_table():
    src = 'conn.execute("UPDATE import_files SET offset=0 WHERE path=?")\n'
    out = lint_imports.check_source(src, "sessionpack/importer.py")
    assert len(out) == 1
    assert "E_SQL" in out[0]


def test_sql_rule_ok_catalog_and_plain_select():
    src = 'conn.execute("UPDATE import_files SET offset=0 WHERE path=?")\n'
    assert lint_imports.check_source(src, "sessionpack/catalog/write.py") == []
    src2 = 'conn.execute("SELECT * FROM sessions WHERE project_id=?")\n'
    assert lint_imports.check_source(src2, "sessionpack/archive.py") == []
    # conn.py 的 SCHEMA 建表 DDL 属合法豁免（CREATE TABLE projects 等）
    src3 = 'SCHEMA = "CREATE TABLE IF NOT EXISTS projects (x)"\n'
    assert lint_imports.check_source(src3, "sessionpack/catalog/conn.py") == []


def test_sql_rule_hits_write_in_non_exempt_catalog_modules():
    """豁免必须是文件级精确：只读子模块/门面里误写受管表写语句仍须报 E_SQL，
    否则 read.py '只做 SELECT' 的承诺没有守卫兜底。"""
    src = 'conn.execute("UPDATE sessions SET title=? WHERE session_id=?")\n'
    out = lint_imports.check_source(src, "sessionpack/catalog/read.py")
    assert any("E_SQL" in o for o in out), "read.py 不应豁免 E_SQL"
    out2 = lint_imports.check_source(src, "sessionpack/catalog/__init__.py")
    assert any("E_SQL" in o for o in out2), "catalog 门面不应豁免 E_SQL"


def test_intra_package_and_relative_import_exempt():
    src = "from .verify import verify_archive\n"
    assert lint_imports.check_source(src, "sessionpack/archive/install.py") == []


def test_dunder_import_is_public_not_private():
    src = "from sessionpack import __version__\nv = __version__\n"
    assert lint_imports.check_source(src, "sessionpack/archive/build.py") == []


def test_intra_package_private_call_exempt_only_inside_that_package():
    src = "from . import build\nx = build._sha256_file(p)\n"
    assert lint_imports.check_source(src, "sessionpack/archive/install.py") == []
    out = lint_imports.check_source(src, "sessionpack/export.py")
    assert any("E_PRIVATE" in o for o in out)


def test_layer_edge_from_module_level_import_not_shadowed_by_symbol():
    src = "from sessionpack.catalog import get_projects\n"
    out = lint_imports.check_source(src, "sessionpack/adapters/trae_memory.py")
    assert any("E_LAYER" in o for o in out)


def test_subpackage_facade_import_still_checked():
    # 子包文件（pkg=sessionpack.archive）经顶层门面 `from sessionpack import X`
    # 导入高层模块：E_LAYER 与 E_PRIVATE 均不得因 pkg!=sessionpack 而漏报。
    out = lint_imports.check_source(
        "from sessionpack import server\n", "sessionpack/archive/install.py")
    assert any("E_LAYER" in o for o in out)
    src = "from sessionpack import catalog\ncatalog._priv(p)\n"
    out2 = lint_imports.check_source(src, "sessionpack/archive/install.py")
    assert any("E_PRIVATE" in o for o in out2)
    out3 = lint_imports.check_source(src, "sessionpack/adapters/trae_memory.py")
    assert any("E_PRIVATE" in o for o in out3)


def test_subpackage_facade_import_low_layer_ok():
    # 子包经门面导入低层合规；dunder 门面名不误报私有。
    assert lint_imports.check_source(
        "from sessionpack import catalog\n",
        "sessionpack/archive/build.py") == []
    assert lint_imports.check_source(
        "from sessionpack import __version__\nv = __version__\n",
        "sessionpack/archive/install.py") == []


def test_check_tree_finds_baseline_violations_on_repo():
    root = Path(__file__).resolve().parents[1]
    found = set(lint_imports.check_tree(root))
    assert found == lint_imports.BASELINE, (
        "基线与当前代码不符。新增违规必须修代码；消除违规必须同步删基线条目。"
        f"\n新增: {sorted(found - lint_imports.BASELINE)}"
        f"\n已消除: {sorted(lint_imports.BASELINE - found)}"
    )
