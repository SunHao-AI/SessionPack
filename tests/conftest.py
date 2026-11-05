import json
from pathlib import Path

import pytest

SID_A = "a" * 24
SID_B = "b" * 24


def _row(message_id: str, ts: str, intent: str, outcome: str,
         actions: list[str], learned: list[str]) -> dict:
    return {
        "message_id": message_id,
        "message_summary_time": ts,
        "intent": intent,
        "outcome": outcome,
        "actions": actions,
        "learned": learned,
    }


def write_jsonl(path: Path, rows: list[dict], extra_raw: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if extra_raw is not None:
        text += extra_raw
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def memory_tree(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    suffixed = root / "projects" / "-d-Work-RepoA--p2-0123456789abcdef"
    plain = root / "projects" / "-d-Work-RepoA"
    other = root / "projects" / "-d-Work-RepoB"

    day_a = suffixed / "20260101"
    day_b = plain / "20260102"
    day_c = other / "20260102"

    day_a.mkdir(parents=True)
    (day_a / "topics.md").write_text(
        f"[session_id: {SID_A} | topic_summary_time: 2026-01-01 10:00:00]实现归档包的校验流程\n"
        "这一行不是合法记录\n",
        encoding="utf-8",
    )
    write_jsonl(
        day_a / f"session_memory_{SID_A}.jsonl",
        [
            _row("m1", "2026-01-01 10:00:00", "导入会话", "完成基线导入",
                 ["读取 topics.md"], ["批量提交比逐文件提交快"]),
            _row("m2", "2026-01-01 10:05:00", "校验归档", "校验通过",
                 ["计算 sha256"], ["清单是唯一可信来源"]),
        ],
    )
    write_jsonl(
        day_b / f"session_memory_{SID_B}.jsonl",
        [
            _row("m1", "2026-01-02 09:00:00", "整理项目", "生成续接摘要",
                 ["提取涉及文件"], ["摘要必须确定性生成"]),
            _row("m2", "2026-01-02 09:10:00", "补充测试", "覆盖率提升",
                 [], []),
        ],
        extra_raw="这不是 JSON\n",
    )
    (day_c / "topics.md").parent.mkdir(parents=True)
    (day_c / "topics.md").write_text(
        f"[session_id: {SID_B} | topic_summary_time: 2026-01-02 11:00:00]跨设备检索准备\n",
        encoding="utf-8",
    )

    (plain / "project_memory.md").write_text("项目长期记忆\n", encoding="utf-8")
    (suffixed / "project_memory.md").write_text("项目长期记忆（旧分片）\n", encoding="utf-8")
    (root / "user_profile.md").write_text("用户偏好\n", encoding="utf-8")
    (root / "trae-jwt-token").write_text("secret-token", encoding="utf-8")
    return root
