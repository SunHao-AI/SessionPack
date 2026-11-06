"""失败信息摘要：OSError 友好化 + 绝对路径脱敏 + 截断。L0 叶子模块。"""
from __future__ import annotations

import re

_PATH_TEXT_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']*|/(?:Users|home|var|tmp)[^\s\"']*")


def failure_text(exc: BaseException, limit: int = 60) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        text = exc.strerror
        if exc.filename:
            text += "（文件位于数据目录内）"
    else:
        text = str(exc)
    return _PATH_TEXT_RE.sub("<路径>", text)[:limit]
