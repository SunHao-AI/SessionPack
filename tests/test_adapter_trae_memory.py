from pathlib import Path

from sessionpack.adapters import TraeMemoryAdapter
from tests.conftest import SID_A, SID_B


def test_declared_paths_include_whitelist_and_exclude_token(memory_tree: Path) -> None:
    declared = TraeMemoryAdapter().declared_paths(memory_tree)
    names = sorted(p.name for p in declared)
    assert names.count("project_memory.md") == 2
    assert "user_profile.md" in names
    assert "topics.md" in names
    assert "trae-jwt-token" not in names
    assert len(declared) == 7


def test_enumerate_merges_suffixed_project_dirs(memory_tree: Path) -> None:
    files = list(TraeMemoryAdapter().enumerate(memory_tree))
    assert {f.project_id for f in files} == {"-d-Work-RepoA", "-d-Work-RepoB"}
    assert {f.kind for f in files} == {"topics", "messages"}
    assert len(files) == 4


def test_inspect_reports_counts_and_dates(memory_tree: Path) -> None:
    info = TraeMemoryAdapter().inspect(memory_tree)
    assert info.adapter_id == "trae-cn-memory"
    assert info.project_count == 2
    assert info.project_dir_count == 3
    assert info.topic_files == 2
    assert info.jsonl_files == 2
    assert info.bytes_total > 0
    assert (info.date_min, info.date_max) == ("2026-01-01", "2026-01-02")


def test_read_file_parses_topics_and_skips_bad_lines(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(f for f in adapter.enumerate(memory_tree) if f.kind == "topics")
    records = [rec for _, rec, err in adapter.read_file(ref) if rec]
    errors = [err for _, _, err in adapter.read_file(ref) if err]
    assert len(records) == 1
    assert records[0].session_id == SID_A
    assert records[0].body == "实现归档包的校验流程"
    assert errors == ["topic format mismatch"]


def test_read_file_resumes_with_continuing_sequence(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(
        f for f in adapter.enumerate(memory_tree)
        if f.kind == "messages" and f.path.name == f"session_memory_{SID_A}.jsonl"
    )
    first = list(adapter.read_file(ref))
    second = list(adapter.read_file(ref, first[0][0], start_seq=1))
    assert [r.seq for _, r, _ in first if r] == [1, 2]
    assert [r.seq for _, r, _ in second if r] == [2]
    assert second[0][1].message_id == "m2"


def test_read_file_records_failure_for_broken_json(memory_tree: Path) -> None:
    adapter = TraeMemoryAdapter()
    ref = next(
        f for f in adapter.enumerate(memory_tree)
        if f.kind == "messages" and f.path.name == f"session_memory_{SID_B}.jsonl"
    )
    results = list(adapter.read_file(ref))
    assert len([r for _, r, _ in results if r]) == 2
    assert len([e for _, _, e in results if e]) == 1
