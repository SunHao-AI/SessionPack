"""模块边界守卫：层级白名单 / 私有符号跨模块 / SQL 独占。仅 stdlib。

用法:  python scripts/lint_imports.py [--update]
--update 会把当前违规写回本文件的 BASELINE（只允许在违规确属预期时手动使用）。
输出格式: 文件:行: 规则名 说明
"""
from __future__ import annotations

import ast
import posixpath
import re
import sys
import tokenize
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "sessionpack"
BASELINE_FILE = Path(__file__)

# §3.2 分层规则表：数字越大层级越高；同层允许。
# 顶层模块与包本身用裸名；包内子模块一律用 "包名.子模块名" 前缀键，
# 避免不同包的同名子模块（catalog.read / server.read）互相串扰。
LAYER_OF: dict[str, int] = {
    "model": 0, "paths": 0, "audit": 0, "mem": 0, "summary": 0,
    "config": 0, "textutil": 0, "crypto": 0,
    "adapters": 1,
    "catalog": 2,
    "catalog.conn": 2, "catalog.read": 2, "catalog.write": 2,
    "archive": 3, "export": 3, "importer": 3, "sync": 3, "diagnostics": 3,
    "rebuild": 3,
    "cli": 4, "server": 4,
    # python -m sessionpack 的入口薄壳，与 cli 同为最高消费层
    "__main__": 4,
    "cli.common": 4, "cli.parser": 4, "cli.read_cmds": 4, "cli.write_cmds": 4,
    # cli/output.py 只依赖 L0（渲染层被各层共用），刻意下沉为 L0
    "cli.output": 0,
}

SQL_WRITE = re.compile(
    r"(INSERT|UPDATE|DELETE FROM|CREATE TABLE).*(projects|sessions|messages"
    r"|import_runs|import_files|message_fts)"
)

# E_SQL 精确豁免集合：以实跑探测确认的最小合法写文件集
# （conn.py 含 SCHEMA 的 CREATE TABLE DDL 1 处，write.py 含写语句 20 处；
# read.py/__init__.py 实测 0 处，不得加入本集合）。
SQL_WRITE_ALLOWED = frozenset({
    "sessionpack/catalog/conn.py",
    "sessionpack/catalog/write.py",
})

# E_DOM_IMPORT：渲染层 cli/output.py 为 cli 包专属（Spec 4 §2）。
# 豁免用包前缀而非 E_SQL 式冻结文件集：cli/ 新增子模块天然合法；
# 其他层产生渲染需求时，正确做法是把需求挪进 CLI 层，而非豁免。
RENDER_TARGET = "sessionpack.cli.output"
RENDER_ALLOWED_PREFIX = "sessionpack/cli/"
# 门面拼写封堵：cli/__init__.py 把 output 子模块绑进了门面命名空间，
# `from sessionpack.cli import output` / `import *` 与直接 import 该子模块
# 在运行时等价，只按点路径字面匹配会被这类拼写静音。
RENDER_FACADE = RENDER_TARGET.rpartition(".")[0]  # sessionpack.cli
RENDER_FACADE_ATTR = RENDER_TARGET.rpartition(".")[2]  # output

# E_PRIVATE 行级豁免注释（Spec 4 §3.1）：只放行该行号上的 E_PRIVATE，
# 不豁免 E_LAYER / E_DOM_IMPORT / E_SQL；被豁免行以 W_ALLOW_PRIVATE 警告
# 输出，防静默滥用。属性链跨多行时报行是 ast.Attribute.lineno（首行），
# 注释须落在该行。
ALLOW_PRIVATE_MARK = "# lint-imports: allow-private"

# 只减不增：修复一个删一行；禁止向其中新增条目来"解决"新违规。
BASELINE: set[str] = set()


def module_of(rel: str) -> tuple[str | None, str | None]:
    """'sessionpack/importer.py' -> ('importer', None)；
    'sessionpack/adapters/trae_memory.py' -> ('adapters', 'trae_memory')；
    'sessionpack/cli/__init__.py' -> ('cli', None)（子包门面归到所属包本身）；
    'sessionpack/__init__.py' -> (None, None)（顶层门面不属于任何模块）。"""
    parts = rel.removesuffix(".py").split("/")
    if parts[0] != "sessionpack" or len(parts) < 2:
        return None, None
    head = parts[1]
    if head == "__init__":
        return None, None
    if parts[-1] == "__init__":
        # 子包门面：按所属包的层参与分层检查，不留 '__init__' 这个
        # 假子模块名（否则会与前缀键查找、跨包同名规则互相干扰）。
        return head, None
    if len(parts) == 2:
        return head, None
    if head == "adapters":
        return "adapters", parts[2]
    return head, parts[-1]  # 未来其他子包：首段为包名


def _layer_of(mod: str, sub: str | None) -> int | None:
    """(包名, 子模块名) -> 层号。

    子模块先查 "包名.子模块名" 前缀键；查不到再回落到包自身的层号
    （adapters 各 adapter、未来新增子模块都走这条回落路径）。
    """
    if sub is not None:
        hit = LAYER_OF.get(f"{mod}.{sub}")
        if hit is not None:
            return hit
    return LAYER_OF.get(mod)


def layer(rel_or_mod: str) -> int | None:
    """模块层级；None 只表示"该路径不是受检模块"（顶层门面/包外路径）。

    已登记但查不到层号（如新出现的子模块）同样返回 None，由调用方按
    未登记（L0 + W_UNMAPPED）处理，不再享有免检特权。
    """
    mod, sub = module_of(rel_or_mod)
    if mod is None:
        return None
    return _layer_of(mod, sub)


def _abs_from_import(node, package: str) -> str:
    """把 ImportFrom 解析为绝对模块点路径（相对导入按源模块所在包解析）。
    package 为源模块所在包的点路径，如 'sessionpack' 或 'sessionpack.adapters'。"""
    if node.level == 0:
        return node.module or ""
    target = package
    for _ in range(node.level - 1):
        target = target.rpartition(".")[0] or "sessionpack"
    return f"{target}.{node.module}" if node.module else target


def _is_dunder(name: str) -> bool:
    """双下划线环绕的名字（如 __version__）是包门面暴露的公共符号。"""
    return name.startswith("__") and name.endswith("__") and len(name) > 4


def _is_private(name: str) -> bool:
    """`_` 前缀视为私有；双下划线环绕的 dunder（如 __version__）是公共契约。"""
    return name.startswith("_") and not _is_dunder(name)


def _is_same_module(owner: str, source_module: str) -> bool:
    """owner 是否就是当前模块自身（同模块内部协作，Spec 5 §5）。

    此处不使用 `owner.endswith("." + source_module)`：段边界对**唯一段**的
    模块名同样会误配深层同名模块——`sessionpack.foo.cli` 对 source_module
    为 'cli' 的模块命中 `endswith(".cli")`，而它并非当前模块，其私有符号
    属跨包访问（N-2）。owner 永远是 `sessionpack.` 开头的完整模块路径，
    故按 `sessionpack.<模块键>` 精确比较；保留裸名相等比较以覆盖
    模块键直接给出的调用方。
    """
    return owner == source_module or owner == f"sessionpack.{source_module}"


def _exempt_lines(src: str, rel: str, warn_sink: list[str]) -> set[int]:
    """收集写有 ALLOW_PRIVATE_MARK 的行号。

    只认真正的注释 token（tokenize.COMMENT）：裸子串扫描会把写在字符串
    字面量里的 marker（x = f() or '# lint-imports: allow-private'）当成
    豁免，等于把私有符号守卫的关闭开关交给了运行时数据。
    tokenize 失败（语法错误源文件）时回退到裸行扫描：check_source 已在
    更前面用 ast.parse 拦下语法错误，这里只是不让豁免收集单独炸掉，宁可
    多豁免也不崩（与本文件其余规则「静态检不出即保守放行」的取态一致）。
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(StringIO(src).readline):
            if tok.type == tokenize.COMMENT and ALLOW_PRIVATE_MARK in tok.string:
                lines.add(tok.start[0])
    except (tokenize.TokenError, SyntaxError, ValueError):
        lines = {no for no, line in enumerate(src.splitlines(), start=1)
                 if ALLOW_PRIVATE_MARK in line}
    for no in sorted(lines):
        warn_sink.append(f"{rel}:{no}: W_ALLOW_PRIVATE 该行由注释豁免 E_PRIVATE")
    return lines


def _dotted(node: ast.AST) -> str | None:
    """纯 Name/Attribute 链拼成点路径，混入 Call/Subscript 等则返回 None。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _rebound_names(tree: ast.AST) -> set[str]:
    """文件内被重新绑定过的名字（赋值/增广赋值/带注解赋值/海象/for/with/
    except-as 目标、函数与 lambda 参数、推导式目标）。属性链根名命中即保守
    放行（Spec 4 §3.2）：同名实例会误报为模块私有访问，无法静态断定时
    宁可漏报不误报。
    取舍在案：遮蔽判定是文件级、不区分作用域，故
    '先 import 模块、后同名重绑定、再跨模块访问私有属性'的真实违例会被
    漏掉——该模式极罕见且本身是坏味道，接受（Spec 5 §2 复核后维持）。
    推导式目标与 lambda 形参在 Spec 5 §2 补入：二者都是局部变量，此前
    未收集会让 `[c._priv() for c in rows]`、`lambda conn: conn._priv()`
    这类合法代码被误报。"""
    names: set[str] = set()

    def _add(t: ast.expr | None) -> None:
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                _add(elt)
        elif isinstance(t, ast.Starred):
            _add(t.value)

    def _add_args(a: ast.arguments) -> None:
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                    a.vararg, a.kwarg]:
            if arg is not None:
                names.add(arg.arg)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _add(t)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            _add(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _add(node.target)
        elif isinstance(node, ast.comprehension):
            _add(node.target)
        elif isinstance(node, ast.withitem):
            _add(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add_args(node.args)
        elif isinstance(node, ast.Lambda):
            _add_args(node.args)
    return names


def check_source(src: str, rel: str,
                 warnings: list[str] | None = None) -> list[str]:
    """对单个文件源码返回违规列表（字符串格式见 BASELINE）。

    warnings 为可选的警告收集列表，用于收集 W_UNMAPPED（不影响退出码）。
    """
    tree = ast.parse(src)
    mod, _ = module_of(rel)
    source_module = mod or "__init__"
    # 源模块所在包：按 rel 的父目录计算，支持任意深度子包
    # （sessionpack/archive/build.py -> sessionpack.archive）
    pkg = posixpath.dirname(rel).replace("/", ".")
    out: list[str] = sql_violations(tree, rel)
    warn_sink = warnings if warnings is not None else []
    exempt_lines = _exempt_lines(src, rel, warn_sink)

    imported_names: dict[str, str] = {}  # 本地名 -> 定义它的绝对模块
    head_bound: set[str] = set()  # 无别名点路径导入绑定的首段根名（Spec 5 §4）
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("sessionpack"):
                    # 别名式导入同样能触到私有模块：import sessionpack.a._b as x
                    segments = a.name.split(".")[1:]
                    hit = next((s for s in segments if _is_private(s)), None)
                    # 豁免条件与 from-import 分支对齐：私有名所在的包就是
                    # 当前模块自身（同包内部协作合法），或所在包是顶层包门面。
                    owner = a.name.rpartition(".")[0]
                    if (hit is not None
                            and not _is_same_module(owner, source_module)
                            and owner != "sessionpack"):
                        out.append(
                            f"{rel}:{node.lineno}: E_PRIVATE 'import' pulls "
                            f"private '{hit}' from '{a.name}'")
                    imported_names[a.asname or a.name.split(".")[0]] = a.name
                    if a.asname is None and "." in a.name:
                        # 无别名点路径导入绑的是首段（import sessionpack.cli
                        # 只把 sessionpack 绑进本地名），Spec 5 §4 用。
                        head_bound.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            amod = _abs_from_import(node, pkg)
            # `from sessionpack import X` / `from . import X`：X 是子模块，
            # 名字归属到 sessionpack.X 而非包本身，否则 `X._y` 检不出来。
            # 顶层包门面 sessionpack 对所有深度的子包同样适用。
            is_pkg_edge = amod in (pkg, "sessionpack")
            for a in node.names:
                local = a.asname or a.name
                if _is_private(a.name):
                    owner = amod.rpartition(".")[0]
                    if (amod.startswith("sessionpack")
                            and not _is_same_module(owner, source_module)
                            and owner != "sessionpack"):
                        out.append(
                            f"{rel}:{node.lineno}: E_PRIVATE 'from import' "
                            f"pulls private '{a.name}' from '{amod}'")
                if amod.startswith("sessionpack"):
                    owner_mod = f"{amod}.{a.name}" if is_pkg_edge else amod
                    imported_names[local] = owner_mod
    for local, owner in list(imported_names.items()):
        # owner 是导入来源的完整模块路径（sessionpack.importer.state），而
        # source_module 只是模块键（'importer'），故同包豁免须按 owner 的包段
        # 与 pkg 比较：否则包内互导私有符号（Spec 4 §4「包内互导同包豁免」）
        # 会被误报为跨模块私有访问。pkg != "sessionpack" 是必要的：对顶层扁平
        # 模块 pkg 恒为 "sessionpack"，不加此条件会顺带关掉扁平模块跨模块私有
        # 导入的机械检测（Task 3 审查 I-1）。
        if (_is_private(local)
                and not _is_same_module(owner, source_module)
                and not (pkg != "sessionpack"
                         and owner.rpartition(".")[0] == pkg)):
            out.append(
                f"{rel}:0: E_PRIVATE module attribute '{local}' imported "
                f"from '{owner}' is private")

    rebound = _rebound_names(tree)
    facade_ok = rel.startswith(RENDER_ALLOWED_PREFIX)
    # 门面属性链：把门面（sessionpack.cli）以任何名字绑进本文件后取 `.output`
    # （`from sessionpack import cli` + `cli.output.render(...)`、
    # `import sessionpack.cli as c` + `c.output.render(...)`、
    # `import sessionpack` + `sessionpack.cli.output.render(...)`），
    # 运行时与直接 import sessionpack.cli.output 等价，同样按渲染层引用处理。
    # Spec 5 §4 两处收窄（均为消 FP 方向，真阳性三种拼写照旧）：
    # 1) 头绑定剔除——`import sessionpack.cli` 只把首段 sessionpack 绑进
    #    本地名，sessionpack 随时可被重绑定成无关对象，且 `sessionpack.output`
    #    不构成渲染层路径（真路径由下方全路径匹配覆盖），故不登记为根；
    # 2) 尊重 rebound——根名在文件内被重绑定过（形参/赋值等）时保守放行，
    #    与 route C 政策对齐。
    if not facade_ok:
        facade_roots = {n for n, o in imported_names.items()
                        if o in (RENDER_FACADE, RENDER_TARGET)
                        and n not in head_bound} - rebound
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute)
                    and node.attr == RENDER_FACADE_ATTR):
                continue
            prefix = _dotted(node.value)
            if (prefix in facade_roots
                    or f"{prefix}.{RENDER_FACADE_ATTR}" == RENDER_TARGET):
                out.append(
                    f"{rel}:{node.lineno}: E_DOM_IMPORT render layer "
                    f"'{RENDER_TARGET}' reached via attribute chain "
                    f"'{_dotted(node)}'")

    # 属性访问: obj._attr(...) / obj._attr / obj.sub._attr(...)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if (isinstance(root, ast.Name) and root.id in imported_names
                    and root.id not in rebound):
                owner = imported_names[root.id]
                owner_tail = owner.removeprefix("sessionpack.").split(".")[0]
                if owner_tail != source_module and owner_tail != "sessionpack":
                    out.append(
                        f"{rel}:{node.lineno}: E_PRIVATE module attribute "
                        f"'{node.attr}' called on '{owner}'")

    # 层级白名单。顶层包门面（sessionpack/__init__.py）与包外路径不属于任何
    # 模块，保持不检；其余未登记模块按 L0 处理，不再静默免检。
    my_layer = layer(rel)
    unmapped_src = my_layer is None and module_of(rel)[0] is not None
    if unmapped_src:
        my_layer = 0
        warn_sink.append(f"{rel}:0: W_UNMAPPED 模块未登记 LAYER_OF，按 L0 处理")
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            amod = _abs_from_import(node, pkg)
            # 门面拼写：output 已被 cli/__init__.py 绑进门面命名空间，
            # `from sessionpack.cli import output` / `import *` 运行时真实可达，
            # 与直接 import sessionpack.cli.output 等价。
            if amod == RENDER_FACADE and not facade_ok:
                facade = next((a.name for a in node.names
                               if a.name in (RENDER_FACADE_ATTR, "*")), None)
                if facade is not None:
                    out.append(
                        f"{rel}:{node.lineno}: E_DOM_IMPORT render layer "
                        f"'{RENDER_TARGET}' imported outside cli/ via "
                        f"'from {RENDER_FACADE} import {facade}'")
            # `from pkg import X` / `from . import X` 的边指向子模块 X；
            # `from pkg.sub import name` 的边就是 pkg.sub 本身（不要拼成
            # pkg.sub.name，否则目标模块被符号名遮蔽，层级边全部漏报）。
            # 顶层包门面对子包文件（pkg=sessionpack.archive 等）同样适用。
            if amod in (pkg, "sessionpack"):
                mods = [f"{amod}.{a.name}" for a in node.names]
            else:
                mods = [amod]
        for m in mods:
            if not m.startswith("sessionpack"):
                continue
            if m == RENDER_TARGET or m.startswith(RENDER_TARGET + "."):
                if not rel.startswith(RENDER_ALLOWED_PREFIX):
                    out.append(
                        f"{rel}:{node.lineno}: E_DOM_IMPORT render layer "
                        f"'{RENDER_TARGET}' imported outside cli/")
            parts = m.split(".")
            # 顶层门面的 dunder 属性（sessionpack.__version__）是符号不是模块，
            # 构成分层边只会制造噪声。
            if len(parts) < 2 or parts[1] == "__init__" or _is_dunder(parts[1]):
                continue
            tgt_top = parts[1]
            tgt = parts[2] if (tgt_top == "adapters" and len(parts) > 2) else tgt_top
            tgt_layer = LAYER_OF.get(tgt, LAYER_OF.get(tgt_top))
            if my_layer is None:
                continue
            if tgt_layer is None:
                warn_sink.append(f"{rel}:{node.lineno}: W_UNMAPPED "
                                 f"依赖目标 '{tgt}' 未登记 LAYER_OF，按 L0 处理")
                tgt_layer = 0
            if parts[1:2] and source_module == tgt_top:
                continue  # 包内/同模块边豁免
            if tgt_layer > my_layer:
                out.append(
                    f"{rel}:{node.lineno}: E_LAYER import "
                    f"L{my_layer} -> L{tgt_layer} ({source_module} -> {tgt})")
    if exempt_lines:
        out = [v for v in out if not ("E_PRIVATE" in v
                                      and int(v.split(":")[1]) in exempt_lines)]
    return sorted(set(out), key=lambda s: (s.split(":")[0], int(s.split(":")[1])))


def sql_violations(tree: ast.AST, rel: str) -> list[str]:
    """SQL 独占令：字符串字面量含受管表写语句，仅 SQL_WRITE_ALLOWED 允许。

    豁免是文件级精确集合（conn.py 的 SCHEMA 建表 DDL、write.py 的增删改），
    catalog 其余文件（read.py/__init__.py）不豁免，保证只读承诺有守卫兜底。

    docstring 只是说明性文字，先收集其节点 id 再跳过，避免「注释里提到
    INSERT INTO」被判成违规。
    """
    if rel in SQL_WRITE_ALLOWED:
        return []
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    out: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings
                and SQL_WRITE.search(node.value)):
            out.append(f"{rel}:{node.lineno}: E_SQL raw SQL write on "
                       "managed table (only catalog conn/write allowed)")
    # SQL 常量可能被变量引用后传入 execute；AST 字面量检索对多段拼接
    # 字符串已在 f-string/常量折叠下可检，若发现漏网请改用 tokenize 全文扫描。
    return out


def check_tree(root: Path) -> list[str]:
    found: list[str] = []
    warnings: list[str] = []
    for path in sorted((root / "sessionpack").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        found.extend(check_source(path.read_text(encoding="utf-8"), rel,
                                  warnings=warnings))
    for line in warnings:
        print(line, file=sys.stderr)
    return found


def _update_baseline(violations: set[str]) -> None:
    text = BASELINE_FILE.read_text(encoding="utf-8")
    if violations:
        body = "{\n" + "".join(f'    "{v}",\n' for v in sorted(violations)) + "}"
    else:
        body = "set()"
    new = re.sub(r"BASELINE: set\[str\] = (?:\{\n.*?\n\}|set\(\))\n",
                 f"BASELINE: set[str] = {body}\n", text, count=1,
                 flags=re.S)
    BASELINE_FILE.write_text(new, encoding="utf-8")


def main(argv: list[str]) -> int:
    found = check_tree(ROOT)
    found_set = set(found)
    if "--update" in argv:
        _update_baseline(found_set)
        print(f"BASELINE updated: {len(found_set)} entries")
        return 0
    for v in found:
        print(v)
    new = found_set - BASELINE
    gone = BASELINE - found_set
    if gone:
        print(f"!! {len(gone)} 条基线违规已消除，请删除 BASELINE 对应条目"
              "（这是好事，说明修复生效）", file=sys.stderr)
    if new:
        print(f"!! {len(new)} 条新增违规（基线只减不增，禁止 --update 绕过）",
              file=sys.stderr)
        return 1
    if gone:
        return 1
    print(f"lint_imports OK ({len(found_set)} 条基线违规，无新增)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
