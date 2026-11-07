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


def test_route_d_deep_same_name_module_not_exempted() -> None:
    """Spec 5 §5（N-2）：route D 的同模块豁免按精确模块路径，非 endswith 后缀。

    cli 包内 `from sessionpack.foo.cli import _priv` 的 owner 是
    sessionpack.foo.cli，只是**末段名字同为 cli**，并非当前模块本身。旧的
    `owner.endswith("." + source_module)` 会把它当作同模块而豁免 route D，
    仅剩上游 from-import 分支兜底；收紧后 route D 独立报出该跨包私有导入
    （纵深防御：不依赖上游路线恰好也查同一名字）。真同包 sessionpack.cli.common
    的私有导入仍须豁免。
    """
    found = lint_imports.check_source(
        "from sessionpack.foo.cli import _priv\n_priv()\n",
        "sessionpack/cli/write_cmds.py")
    assert any("E_PRIVATE" in item
               and "module attribute '_priv' imported from "
                    "'sessionpack.foo.cli'" in item
               for item in found), "深层同名模块被 endswith 误判为同模块而漏报"
    # 真同包子模块私有导入仍豁免（:0: route D 不报）
    assert lint_imports.check_source(
        "from sessionpack.cli.common import _helper\n_helper()\n",
        "sessionpack/cli/write_cmds.py") == []


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


def test_dom_import_rule_hits_facade_from_import():
    """`from sessionpack.cli import output` 真实可达（cli/__init__.py 已把
    output 子模块绑进门面命名空间），拼写不同不等于没引用渲染层。"""
    out = lint_imports.check_source(
        "from sessionpack.cli import output\n", "sessionpack/server.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/server.py:1: E_DOM_IMPORT")
    assert "sessionpack.cli.output" in dom[0]


def test_dom_import_rule_hits_facade_star_import():
    """`from sessionpack.cli import *` 同样绕过按点路径拼写的字面匹配。"""
    out = lint_imports.check_source(
        "from sessionpack.cli import *\n", "sessionpack/server.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/server.py:1: E_DOM_IMPORT")


def test_dom_import_rule_hits_facade_attribute_chain():
    """import 门面后属性链取 output（sessionpack.cli.output.render(...)）也要报。"""
    out = lint_imports.check_source(
        "import sessionpack.cli\n\nsessionpack.cli.output.render(x)\n",
        "sessionpack/server.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/server.py:3: E_DOM_IMPORT")


def test_dom_import_rule_hits_facade_attribute_chain_via_package_facade():
    """`from sessionpack import cli` 后 `cli.output.render(...)` 同样可达。"""
    out = lint_imports.check_source(
        "from sessionpack import cli\n\ncli.output.render(x)\n",
        "sessionpack/export.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/export.py:3: E_DOM_IMPORT")


def test_dom_import_rule_hits_aliased_output_import():
    """import sessionpack.cli.output as o 由 E_DOM_IMPORT 直接按点路径命中。"""
    out = lint_imports.check_source(
        "import sessionpack.cli.output as o\n", "sessionpack/export.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/export.py:1: E_DOM_IMPORT")
    assert lint_imports.check_source(
        "import sessionpack.cli.output as o\n", "sessionpack/cli/scratch.py") == []


def test_dom_import_facade_spellings_stay_legal_inside_cli():
    """cli 包内互导门面是正常写法：新堵的三种拼写在 cli/ 下都不得变红。"""
    assert lint_imports.check_source(
        "from sessionpack.cli import output\noutput.emit(x)\n",
        "sessionpack/cli/read_cmds.py") == []
    assert lint_imports.check_source(
        "from . import output\noutput.emit(x)\n",
        "sessionpack/cli/write_cmds.py") == []
    assert lint_imports.check_source(
        "import sessionpack.cli\nsessionpack.cli.output.render(x)\n",
        "sessionpack/cli/parser.py") == []


def test_facade_chain_respects_rebound_param() -> None:
    """Spec 5 §4（Minor-2）：门面根名被形参遮蔽时保守放行，对齐 route C。

    handler 内的 cli 是运行时对象，`cli.output.render()` 与渲染层无关。
    """
    src = ("from sessionpack import cli\n"
           "def handler(cli):\n"
           "    cli.output.render()\n")
    out = lint_imports.check_source(src, "sessionpack/export.py")
    assert not any("E_DOM_IMPORT" in o for o in out)


def test_facade_chain_head_bound_not_a_root() -> None:
    """Spec 5 §4（Minor-1）：`import sessionpack.cli` 绑的是首段 sessionpack，
    不得登记为门面根——sessionpack.output 不是渲染层路径
    （真路径 sessionpack.cli.output 由全路径匹配覆盖，另行验证）。"""
    src = ("import sessionpack.cli\n"
           "sessionpack = load_cfg()\n"
           "sessionpack.output()\n")
    out = lint_imports.check_source(src, "sessionpack/export.py")
    assert not any("E_DOM_IMPORT" in o for o in out)


def test_facade_chain_aliased_head_is_still_a_root() -> None:
    """头绑定剔除不误伤别名拼写：`import sessionpack.cli as c` 的 c 绑的是
    cli 模块本身，`c.output.render()` 是真阳性仍须报。"""
    out = lint_imports.check_source(
        "import sessionpack.cli as c\n\nc.output.render(x)\n",
        "sessionpack/export.py")
    dom = [o for o in out if "E_DOM_IMPORT" in o]
    assert len(dom) == 1
    assert dom[0].startswith("sessionpack/export.py:3: E_DOM_IMPORT")


def test_facade_chain_full_path_unaffected_by_head_bound() -> None:
    """头绑定剔除后真路径 sessionpack.cli.output.x 仍被全路径匹配兜住，
    即便根名 sessionpack 随后被重绑定（该分支不经 facade_roots）。"""
    src = ("import sessionpack.cli\n"
           "sessionpack = load_cfg()\n"
           "sessionpack.cli.output.render(x)\n")
    out = lint_imports.check_source(src, "sessionpack/export.py")
    assert any("E_DOM_IMPORT" in o for o in out)


def test_allow_private_in_string_literal_is_not_an_exemption():
    """marker 只认注释：写在字符串字面量里的 marker 不得放行 E_PRIVATE，
    否则一行代码即可静默绕过私有符号守卫。"""
    src = ("from sessionpack import archive\n"
           "x = archive._p() or '# lint-imports: allow-private'\n")
    warns: list[str] = []
    out = lint_imports.check_source(src, "sessionpack/sync.py", warns)
    assert any("E_PRIVATE" in o for o in out), "字符串字面量造成了假豁免"
    assert not any("W_ALLOW_PRIVATE" in w for w in warns)


def test_allow_private_only_exempts_private_rule_on_that_line():
    """同一行同时踩 E_LAYER + E_PRIVATE：marker 只放行 E_PRIVATE。"""
    src = ("import sessionpack.server as srv; srv._priv()  "
           "# lint-imports: allow-private\n")
    out = lint_imports.check_source(src, "sessionpack/importer.py")
    assert any("E_LAYER" in o for o in out)
    assert not any("E_PRIVATE" in o for o in out)


def test_allow_private_does_not_exempt_dom_import_on_same_line():
    """同一行同时踩 E_DOM_IMPORT + E_PRIVATE：marker 只放行 E_PRIVATE。"""
    src = ("from sessionpack.cli.output import _emit  "
           "# lint-imports: allow-private\n")
    out = lint_imports.check_source(src, "sessionpack/export.py")
    assert any("E_DOM_IMPORT" in o for o in out)
    assert not any(":1: E_PRIVATE" in o for o in out)


def test_allow_private_does_not_exempt_route_d_zero_line_report():
    """route D（导入名本身私有，报 :0:）不受行级豁免影响：marker 换不到放行。"""
    src = ("from sessionpack.archive import _priv  "
           "# lint-imports: allow-private\n")
    out = lint_imports.check_source(src, "sessionpack/cli/read_cmds.py")
    assert any(o.startswith("sessionpack/cli/read_cmds.py:0: E_PRIVATE")
               for o in out)
    assert not any(":1: E_PRIVATE" in o for o in out)


def test_private_rule_ok_module_name_rebound_to_instance():
    """遮蔽豁免：import 进来的名字在文件内被重绑定（赋值/参数等），
    属性链根名无法静态断定是模块引用，保守放行（Spec 4 §3.2）。"""
    src = ("from sessionpack import export\n"
           "export = make_thing()\n"
           "x = export._render()\n")
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    assert not any("E_PRIVATE" in o for o in out)


def test_private_rule_still_hits_names_without_rebinding():
    """收紧是名字级的：被遮蔽的 export 放行，未遮蔽的 archive 照常报。"""
    src = ("from sessionpack import archive\n"
           "def handle(export):\n"
           "    export._render()\n"
           "    return archive._failure_text(e)\n")
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    priv = [o for o in out if "E_PRIVATE" in o]
    assert len(priv) == 1
    assert priv[0].startswith("sessionpack/sync.py:4: E_PRIVATE")


def test_private_line_exempt_by_comment():
    """行级豁免：行尾注释放行该行 E_PRIVATE，且输出 W_ALLOW_PRIVATE 警告。"""
    src = ("from sessionpack import archive\n"
           "x = archive._failure_text(e)  # lint-imports: allow-private\n")
    warns: list[str] = []
    out = lint_imports.check_source(src, "sessionpack/sync.py", warns)
    assert not any("E_PRIVATE" in o for o in out)
    assert any("W_ALLOW_PRIVATE" in w for w in warns)


def test_allow_private_comment_on_clean_line_is_harmless():
    """注释加在无违规行：不产生违规，仍出警告（评审可见）。"""
    src = ("from sessionpack import archive\n"
           "x = archive.build(p)  # lint-imports: allow-private\n")
    warns: list[str] = []
    out = lint_imports.check_source(src, "sessionpack/sync.py", warns)
    assert out == []
    assert any("W_ALLOW_PRIVATE" in w for w in warns)


def test_package_intra_module_absolute_private_import_exempt() -> None:
    """拆包后包内互导（Task 3 §4）：绝对路径 from-import 私有符号放行。

    run.py 里 `from sessionpack.importer.state import _FileState` 是同包
    内部协作，不得报 E_PRIVATE；同样的导入换到包外文件仍须报。
    """
    src = "from sessionpack.importer.state import _FileState\n"
    assert lint_imports.check_source(src, "sessionpack/importer/run.py") == []
    out = lint_imports.check_source(src, "sessionpack/cli/read_cmds.py")
    assert any("E_PRIVATE" in item for item in out)


def test_flat_module_private_import_still_checked_after_I1() -> None:
    """I-1 收窄回归：顶层扁平模块间 from-import 私有仍须机械检出。

    同包豁免的包段比较若不加 `pkg != "sessionpack"`，扁平模块
    （pkg 恒为 sessionpack）会命中该豁免而整体漏报这条唯一的机械检出路径。
    """
    src = "from sessionpack.archive import _write_row\n"
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    assert any("E_PRIVATE" in item for item in out)


def test_rebound_collects_comprehension_target() -> None:
    """Spec 5 §2：推导式目标是局部变量，须计入遮蔽集合。

    `[catalog for catalog in rows]` 之后 `catalog._priv()` 访问的是运行时
    元素而非模块，此前目标未收集会误报 E_PRIVATE。
    """
    src = ("from sessionpack import catalog\n"
           "rows = [c for c in raw]\n"
           "vals = [catalog._priv() for catalog in rows]\n")
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    assert not any("E_PRIVATE" in o for o in out)


def test_rebound_collects_lambda_params() -> None:
    """Spec 5 §2：lambda 形参是局部变量，体内同名私有访问不得误报。"""
    src = ("from sessionpack import catalog\n"
           "f = lambda catalog: catalog._priv()\n")
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    assert not any("E_PRIVATE" in o for o in out)


def test_rebound_shadow_fix_does_not_hollow_route_c() -> None:
    """补收集只放宽遮蔽侧：未遮蔽的模块私有访问仍须报，且同文件内
    遮蔽名与未遮蔽名互不干扰（名字级判定不回归）。"""
    src = ("from sessionpack import archive, catalog\n"
           "vals = [catalog._priv() for catalog in rows]\n"
           "x = archive._failure_text(e)\n")
    out = lint_imports.check_source(src, "sessionpack/sync.py")
    priv = [o for o in out if "E_PRIVATE" in o]
    assert len(priv) == 1
    assert "archive" in priv[0] and "catalog" not in priv[0]
