from __future__ import annotations

import re

FILE_RE = re.compile(
    r"(?:[A-Za-z]:[/\\])?[A-Za-z0-9_./\\-]*[A-Za-z0-9_-]+\.(?:py|ts|tsx|js|jsx|json|md|sql|toml|ya?ml|css|html)\b",
    re.IGNORECASE,
)

ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\])")


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split("|") if part]


def extract_file_mentions(texts: list[str]) -> list[str]:
    found: set[str] = set()
    for text in texts:
        for match in FILE_RE.findall(text or ""):
            normalized = match.replace("\\", "/")
            if ABSOLUTE_RE.match(match):
                normalized = normalized.rsplit("/", 1)[-1]
            while normalized.startswith("./"):
                normalized = normalized[2:]
            while normalized.startswith("../"):
                normalized = normalized[3:]
            found.add(normalized)
    return sorted(found)


def build_summary(session: dict, messages: list[dict]) -> dict:
    learned: list[str] = []
    actions: list[str] = []
    latest_outcome = ""
    for message in messages:
        for item in split_list(message.get("learned")):
            if item not in learned:
                learned.append(item)
        for item in split_list(message.get("actions")):
            if item not in actions:
                actions.append(item)
        if message.get("outcome"):
            latest_outcome = message["outcome"]
    files = extract_file_mentions(actions + learned + [latest_outcome])
    return {
        "session_id": session["session_id"],
        "project_id": session["project_id"],
        "project_name": session.get("display_name") or session["project_id"],
        "title": session.get("title") or "（无标题）",
        "topic": session.get("summary") or "",
        "started_at": session.get("created_at") or "",
        "updated_at": session.get("updated_at") or "",
        "message_count": len(messages),
        "latest_outcome": latest_outcome,
        "learned": learned,
        "actions": actions,
        "files": files,
    }


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- （无记录）"]


def render_markdown(session: dict, messages: list[dict]) -> str:
    data = build_summary(session, messages)
    lines = [
        f"# 续接摘要：{data['title']}",
        "",
        f"- 项目：{data['project_name']}（{data['project_id']}）",
        f"- 会话：{data['session_id']}",
        f"- 更新时间：{data['updated_at']}",
        f"- 消息：{data['message_count']} 条",
        "",
        "## 主题",
        "",
        data["topic"] or "（无主题摘要）",
        "",
        "## 最新进展",
        "",
        data["latest_outcome"] or "（无记录）",
        "",
        "## 关键结论",
        "",
        *_bullets(data["learned"]),
        "",
        "## 已执行动作",
        "",
        *_bullets(data["actions"]),
        "",
        "## 涉及文件",
        "",
        *_bullets([f"`{path}`" for path in data["files"]]),
        "",
    ]
    return "\n".join(lines)
