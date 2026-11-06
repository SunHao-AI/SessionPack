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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "sessionpack"
BASELINE_FILE = Path(__file__)

# §3.2 分层规则表：数字越大层级越高；同层允许。
LAYER_OF: dict[str, int] = {
    "model": 0, "paths": 0, "audit": 0, "mem": 0, "summary": 0,
    "config": 0, "textutil": 0,
    "adapters": 1,
    "catalog": 2,
    "conn": 2,
    "read": 2,
    "write": 2,
    "archive": 3, "export": 3, "importer": 3, "sync": 3, "diagnostics": 3,
    "cli": 4, "server": 4,
    "output": 0,
    "common": 4,
    "parser": 4,
    "read_cmds": 4,
    "write_cmds": 4,
}

SQL_WRITE = re.compile(
    r"(INSERT|UPDATE|DELETE FROM|CREATE TABLE).*(projects|sessions|messages"
    r"|import_runs|import_files|message_fts)"
)

# E_SQL 精确豁免集合：以实跑探测确认的最小合法写文件集
# （conn.py 含 SCHEMA 的 CREATE TABLE DDL 1 处，write.py 含写语句 15 处；
# read.py/__init__.py 实测 0 处，不得加入本集合）。
SQL_WRITE_ALLOWED = frozenset({
    "sessionpack/catalog/conn.py",
    "sessionpack/catalog/write.py",
})

# 只减不增：修复一个删一行；禁止向其中新增条目来"解决"新违规。
BASELINE: set[str] = set()


def module_of(rel: str) -> tuple[str | None, str | None]:
    """'sessionpack/importer.py' -> ('importer', None)；
    'sessionpack/adapters/trae_memory.py' -> ('adapters', 'trae_memory')；
    'sessionpack/__init__.py' -> (None, None)。"""
    parts = rel.removesuffix(".py").split("/")
    if parts[0] != "sessionpack" or len(parts) < 2:
        return None, None
    head = parts[1]
    if head == "__init__":
        return None, None
    if len(parts) == 2:
        return head, None
    if head == "adapters":
        return "adapters", parts[2]
    return head, parts[-1]  # 未来其他子包：首段为包名


def layer(rel_or_mod: str) -> int | None:
    mod, sub = module_of(rel_or_mod)
    if mod is None:
        return None
    return LAYER_OF.get(sub or mod, LAYER_OF.get(mod))


def _abs_from_import(node, package: str) -> str:
    """把 ImportFrom 解析为绝对模块点路径（相对导入按源模块所在包解析）。
    package 为源模块所在包的点路径，如 'sessionpack' 或 'sessionpack.adapters'。"""
    if node.level == 0:
        return node.module or ""
    target = package
    for _ in range(node.level - 1):
        target = target.rpartition(".")[0] or "sessionpack"
    return f"{target}.{node.module}" if node.module else target


def _is_private(name: str) -> bool:
    """`_` 前缀视为私有；双下划线环绕的 dunder（如 __version__）是公共契约。"""
    return name.startswith("_") and not (
        name.startswith("__") and name.endswith("__") and len(name) > 4)


def check_source(src: str, rel: str) -> list[str]:
    """对单个文件源码返回违规列表（字符串格式见 BASELINE）。"""
    tree = ast.parse(src)
    mod, _ = module_of(rel)
    source_module = mod or "__init__"
    # 源模块所在包：按 rel 的父目录计算，支持任意深度子包
    # （sessionpack/archive/build.py -> sessionpack.archive）
    pkg = posixpath.dirname(rel).replace("/", ".")
    out: list[str] = sql_violations(tree, rel)

    imported_names: dict[str, str] = {}  # 本地名 -> 定义它的绝对模块
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("sessionpack"):
                    # 别名式导入同样能触到私有模块：import sessionpack.a._b as x
                    segments = a.name.split(".")[1:]
                    hit = next((s for s in segments if _is_private(s)), None)
                    if hit is not None:
                        out.append(
                            f"{rel}:{node.lineno}: E_PRIVATE 'import' pulls "
                            f"private '{hit}' from '{a.name}'")
                    imported_names[a.asname or a.name.split(".")[0]] = a.name
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
                            and not owner.endswith(source_module)
                            and owner != "sessionpack"):
                        out.append(
                            f"{rel}:{node.lineno}: E_PRIVATE 'from import' "
                            f"pulls private '{a.name}' from '{amod}'")
                if amod.startswith("sessionpack"):
                    owner_mod = f"{amod}.{a.name}" if is_pkg_edge else amod
                    imported_names[local] = owner_mod
    for local, owner in list(imported_names.items()):
        if _is_private(local) and not owner.endswith(source_module):
            out.append(
                f"{rel}:0: E_PRIVATE module attribute '{local}' imported "
                f"from '{owner}' is private")

    # 属性访问: obj._attr(...) / obj._attr
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            v = node.value
            if isinstance(v, ast.Name) and v.id in imported_names:
                owner = imported_names[v.id]
                owner_tail = owner.removeprefix("sessionpack.").split(".")[0]
                if owner_tail != source_module and owner_tail != "sessionpack":
                    out.append(
                        f"{rel}:{node.lineno}: E_PRIVATE module attribute "
                        f"'{node.attr}' called on '{owner}'")

    # 层级白名单
    my_layer = layer(rel)
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            amod = _abs_from_import(node, pkg)
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
            parts = m.split(".")
            if len(parts) < 2 or parts[1] == "__init__":
                continue
            tgt_top = parts[1]
            tgt = parts[2] if (tgt_top == "adapters" and len(parts) > 2) else tgt_top
            tgt_layer = LAYER_OF.get(tgt, LAYER_OF.get(tgt_top))
            if my_layer is None or tgt_layer is None:
                continue
            if parts[1:2] and source_module == tgt_top:
                continue  # 包内/同模块边豁免
            if tgt_layer > my_layer:
                out.append(
                    f"{rel}:{node.lineno}: E_LAYER import "
                    f"L{my_layer} -> L{tgt_layer} ({source_module} -> {tgt})")
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
    for path in sorted((root / "sessionpack").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        found.extend(check_source(path.read_text(encoding="utf-8"), rel))
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
