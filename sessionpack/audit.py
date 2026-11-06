from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal


class ReadBoundaryError(RuntimeError):
    def __init__(self, unexpected: list[str]) -> None:
        self.unexpected = tuple(sorted(unexpected))
        super().__init__("读取白名单外的文件: " + ", ".join(self.unexpected))


class ReadAudit:
    """基于 sys.addaudithook 的读取边界审计。

    审计钩子一旦安装无法卸载，因此只记录 root 之下的打开事件，
    其他路径（索引库、pytest 自身文件）不参与判定。
    """

    def __init__(self, root: Path, allowed: list[Path]) -> None:
        self.root = Path(root).resolve()
        self.allowed = {str(Path(path).resolve()).casefold() for path in allowed}
        self.opened: set[str] = set()
        self.violations: list[str] = []
        self.active = True

    def __enter__(self) -> ReadAudit:
        sys.addaudithook(self._hook)
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        self.active = False
        return False

    def _hook(self, event: str, args: tuple) -> None:
        if not self.active or event != "open" or not args:
            return
        raw = args[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if not isinstance(raw, str):
            return
        try:
            resolved = Path(raw).resolve()
        except (OSError, ValueError):
            return
        if resolved != self.root and self.root not in resolved.parents:
            return
        key = str(resolved).casefold()
        if key in self.allowed:
            self.opened.add(key)
        else:
            rel = resolved.relative_to(self.root).as_posix()
            if rel not in self.violations:
                self.violations.append(rel)

    def check(self, declared: list[Path]) -> None:
        allowed = {str(Path(path).resolve()).casefold() for path in declared}
        unknown = self.opened - allowed
        if self.violations or unknown:
            raise ReadBoundaryError(self.violations + sorted(unknown))


def audit_summary(audit: ReadAudit, declared: list[Path]) -> dict:
    return {
        "declared": len(declared),
        "opened": len(audit.opened),
        "violations": list(audit.violations),
    }
