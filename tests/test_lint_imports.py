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


def test_import_alias_escape_is_private_violation() -> None:
    """import sessionpack.x._m as alias 也须被 E_PRIVATE 拦住。

    用例刻意只调用公开属性 load()，确保红点来自「导入语句本身触到私有模块」，
    而不是被后续的私有属性规则顺带抓到。
    """
    src = "import sessionpack.catalog._helper as h\n\nh.load()\n"
    found = lint_imports.check_source(src, "sessionpack/cli/scratch.py")
    assert any("E_PRIVATE" in item for item in found)


def test_import_alias_intra_package_private_is_exempt() -> None:
    """同包私有模块可用：import 别名分支的豁免须与 from-import 分支对齐。

    catalog/read.py 里 import sessionpack.catalog._helper 属同包内部协作，
    不得报 E_PRIVATE；换成跨包文件仍须报。
    """
    src = "import sessionpack.catalog._helper as h\n\nh.load()\n"
    assert lint_imports.check_source(src, "sessionpack/catalog/read.py") == []
    found = lint_imports.check_source(src, "sessionpack/cli/scratch.py")
    assert any("E_PRIVATE" in item for item in found)


def test_name_collision_package_is_not_exempted_as_same_package() -> None:
    """撞名包不得享同包豁免：豁免必须按段边界比较，不能用 endswith 后缀匹配。

    catalog/read.py 里 `from sessionpack.data_catalog._x import _priv` 的 owner
    是 sessionpack.data_catalog，只是**名字以 catalog 结尾**，并非同包，
    必须报 E_PRIVATE；真正的同包 `sessionpack.catalog._helper` 仍须豁免。
    """
    found = lint_imports.check_source(
        "from sessionpack.data_catalog._x import _priv\n",
        "sessionpack/catalog/read.py")
    assert any("E_PRIVATE" in item and "'from import'" in item
               for item in found), "撞名包 data_catalog 被误判为同包而漏报"
    assert lint_imports.check_source(
        "from sessionpack.catalog._helper import load\n",
        "sessionpack/catalog/read.py") == []


def test_docstring_mentioning_write_is_not_sql_violation() -> None:
    """docstring 里解释「禁止 INSERT」不该被判成裸 SQL 写入。"""
    src = ('"""本模块不得写库：禁止 INSERT INTO messages。"""\n\n'
           'VALUE = 1\n')
    found = lint_imports.check_source(src, "sessionpack/export/anything.py")
    assert found == []


def test_unmapped_module_is_treated_as_l0_with_warning() -> None:
    """未登记模块按 L0 处理：向上依赖要报错，同时给出 W_UNMAPPED 警告。"""
    warnings: list[str] = []
    src = "from sessionpack import archive\n"
    found = lint_imports.check_source(
        src, "sessionpack/mystery/thing.py", warnings=warnings)
    assert any("E_LAYER" in item for item in found)
    assert any("W_UNMAPPED" in item for item in warnings)


def test_private_attr_on_nested_attribute_chain_is_caught() -> None:
    """mod.sub._priv() 的根名也要解析，不能只看直接 Name。"""
    src = ("from sessionpack import archive as ar\n\n"
           "ar.build._write_row(1)\n")
    found = lint_imports.check_source(src, "sessionpack/cli/scratch.py")
    assert any("E_PRIVATE" in item and "_write_row" in item for item in found)


def test_submodule_layer_is_scoped_to_owning_package(monkeypatch) -> None:
    """子模块层级必须以包为前缀，不能被别的包的同名子模块串扰。"""
    # 新包 export 登记为 L1，其 write.py 与 catalog.write 同名
    monkeypatch.setitem(lint_imports.LAYER_OF, "export", 1)
    # export(L1) -> catalog(L2) 是向上依赖，必须报；旧扁平键 write=2 会误放行
    found = lint_imports.check_source(
        "from sessionpack import catalog\n", "sessionpack/export/write.py")
    assert any("E_LAYER" in item for item in found)


def test_submodule_without_entry_falls_back_to_own_package(monkeypatch) -> None:
    """未显式登记的子模块回落到自己包的层，不受同名他包子模块影响。"""
    # server 为 L4，其 read.py 与 catalog.read(L2) 同名；server -> archive(L3) 合法
    found = lint_imports.check_source(
        "from sessionpack import archive\n", "sessionpack/server/read.py")
    assert found == []


def test_subpackage_facade_is_layer_checked() -> None:
    """子包门面 __init__.py 按所属包的层参与分层检查（不得整包免检）。"""
    # 门面归一到所属包本身（子模块位留空），层级取包层号
    assert lint_imports.module_of("sessionpack/cli/__init__.py") == ("cli", None)
    assert lint_imports.layer("sessionpack/cli/__init__.py") == 4
    # catalog(L2) 门面反向依赖 archive(L3)：必须点出
    found = lint_imports.check_source(
        "from sessionpack import archive\n", "sessionpack/catalog/__init__.py")
    assert any("E_LAYER" in item and "L2 -> L3" in item for item in found)
    # 顶层门面 sessionpack/__init__.py 不属于任何模块，保持不检
    assert lint_imports.module_of("sessionpack/__init__.py") == (None, None)
    assert lint_imports.check_source(
        "from sessionpack import archive, cli\n",
        "sessionpack/__init__.py") == []


def test_repo_tree_has_no_unmapped_warnings(capsys) -> None:
    """全仓实跑不得出现 W_UNMAPPED：所有子模块都已登记 LAYER_OF，
    顶层门面的 dunder 符号（__version__）不构成分层边。"""
    root = Path(__file__).resolve().parents[1]
    lint_imports.check_tree(root)
    assert "W_UNMAPPED" not in capsys.readouterr().err


def test_check_tree_finds_baseline_violations_on_repo():
    root = Path(__file__).resolve().parents[1]
    found = set(lint_imports.check_tree(root))
    assert found == lint_imports.BASELINE, (
        "基线与当前代码不符。新增违规必须修代码；消除违规必须同步删基线条目。"
        f"\n新增: {sorted(found - lint_imports.BASELINE)}"
        f"\n已消除: {sorted(lint_imports.BASELINE - found)}"
    )


def test_dom_import_rule_hits_outside_cli():
    src = "from sessionpack.cli.output import emit\n"
    out = lint_imports.check_source(src, "sessionpack/export.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/export.py:1: E_DOM_IMPORT")


def test_dom_import_rule_ok_inside_cli():
    src = "from sessionpack.cli.output import emit\n"
    assert lint_imports.check_source(src, "sessionpack/cli/read_cmds.py") == []
    src2 = "from .output import emit\n"
    assert lint_imports.check_source(src2, "sessionpack/cli/write_cmds.py") == []


def test_dom_import_rule_hits_relative_import_outside_cli():
    src = "from .cli.output import emit\n"
    out = lint_imports.check_source(src, "sessionpack/server.py")
    assert any("E_DOM_IMPORT" in o for o in out)
