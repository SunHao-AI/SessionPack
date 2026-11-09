"""run_xxx 直接返回 CommandResult，不必经子进程即可断言文案与退出码。"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from sessionpack import archive, catalog, config, crypto
from sessionpack.cli import build_parser, main
from sessionpack.cli.output import CommandResult, emit
from sessionpack.cli.read_cmds import run_doctor, run_scan, run_verify
from sessionpack.cli.write_cmds import (
    run_export,
    run_import,
    run_pull,
    run_push,
    run_rebuild,
    run_reencrypt,
)
from sessionpack.model import MessageRecord, ProjectRecord


def _ns(**kwargs) -> argparse.Namespace:
    """构造与 parser 等价的 args 对象，避免每个用例重复铺默认值。"""
    base = dict(memory_root=None, data_dir=None, json=False, encrypt=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def test_emit_text_lines_split_stdout_and_stderr(capsys) -> None:
    code = emit(CommandResult(2, out_lines=["出"], err_lines=["错"]), as_json=False)
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == "出\n"
    assert captured.err == "错\n"


def test_emit_json_when_data_present(capsys) -> None:
    code = emit(CommandResult(0, out_lines=["不该出现"], data={"a": 1}), as_json=True)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == '{\n  "a": 1\n}\n'
    assert captured.err == ""


def test_emit_ignores_json_flag_when_data_is_none(capsys) -> None:
    """data 为 None 时 as_json 不生效，恒走文本/错误行。"""
    emit(CommandResult(1, err_lines=["只有错误行"]), as_json=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "只有错误行\n"


def test_run_scan_missing_root_is_usage_error(tmp_path: Path) -> None:
    res = run_scan(_ns(memory_root=str(tmp_path / "nope")))
    assert res.exit_code == 2
    assert res.err_lines == [f"记忆目录不存在: {tmp_path / 'nope'}"]
    assert res.out_lines == []
    assert res.data is None


def test_run_doctor_missing_memory_root_text_and_json(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    data_dir = _data_dir(tmp_path)

    res = run_doctor(_ns(memory_root=str(missing), data_dir=str(data_dir)))
    assert res.exit_code == 2
    assert res.data is None
    assert res.out_lines[1] == f"记忆目录：{missing}（不存在）"

    res_json = run_doctor(_ns(memory_root=str(missing), data_dir=str(data_dir),
                              json=True))
    assert res_json.exit_code == 2
    assert res_json.data["memory_root_exists"] is False
    assert res_json.out_lines == []


def test_run_doctor_text_shows_sync_dir(tmp_path: Path) -> None:
    """Spec 6 CFG-3：文本 doctor 显示同步目录状态。"""
    data_dir = _data_dir(tmp_path)
    res = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir)))
    assert any(line == "同步目录：未配置" for line in res.out_lines)

    (data_dir / "config.toml").write_text(
        '[sync]\ndir = "D:\\\\share"\n', encoding="utf-8")
    res2 = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                          data_dir=str(data_dir)))
    assert any("同步目录：D:\\share" == line for line in res2.out_lines)


def test_run_doctor_text_shows_sync_warnings(tmp_path: Path) -> None:
    """Spec 6 CFG-3：config 解析失败时文本 doctor 逐行输出同步警告。"""
    data_dir = _data_dir(tmp_path)
    (data_dir / "config.toml").write_text("[sync\nbroken", encoding="utf-8")
    res = run_doctor(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir)))
    assert any("同步警告：" in line and "解析失败" in line
               for line in res.out_lines)


def test_run_import_missing_memory_root_is_usage_error(tmp_path: Path) -> None:
    res = run_import(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(_data_dir(tmp_path)), archive=None,
                         project=None, since=None, until=None, reparse=False,
                         retry_failed=False, audit=False))
    assert res.exit_code == 2
    assert res.err_lines == [f"记忆目录不存在: {tmp_path / 'nope'}"]


def test_run_export_without_catalog_is_usage_error(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    (data_dir / "catalog.sqlite").write_bytes(b"")
    res = run_export(_ns(data_dir=str(data_dir), out=str(tmp_path / "out"),
                         archive=False, session=None, project=None))
    assert res.exit_code == 2
    assert res.err_lines == ["索引不存在，请先运行 sessionpack import"]


def test_run_verify_marks_bad_archive(tmp_path: Path, monkeypatch) -> None:
    class _Issue:
        def __init__(self, code: str, detail: str) -> None:
            self.code = code
            self.detail = detail

    class _Result:
        def __init__(self, path: Path, ok: bool) -> None:
            self.path = path
            self.ok = ok
            self.manifest = {"archive_id": "a1"} if ok else None
            self.issues = [] if ok else [_Issue("HASH_MISMATCH", "摘要不符")]

    monkeypatch.setattr(archive, "verify_archive",
                        lambda raw: _Result(Path(raw), Path(raw).name == "good.zip"))
    good = tmp_path / "good.zip"
    bad = tmp_path / "bad.zip"
    res = run_verify(_ns(archives=[str(good), str(bad)]))
    assert res.exit_code == 3
    assert res.out_lines == [f"OK  {good}（archive_id a1）",
                             f"BAD {bad}：HASH_MISMATCH"]


def test_run_push_dry_run_and_real(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()

    dry = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=True))
    assert dry.exit_code == 0
    assert dry.out_lines[0].startswith("[dry-run] 将推送 0 个包共 0 字节")

    real = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=False))
    assert real.exit_code == 0
    assert real.out_lines[0].startswith("已推送 0 个包共 0 字节")


def test_run_push_missing_dir_includes_config_diagnosis(tmp_path: Path) -> None:
    """Spec 6 CFG-2：段名拼错时错误输出含诊断，不再只说"未指定"。"""
    data_dir = _data_dir(tmp_path)
    (data_dir / "config.toml").write_text('[snyc]\ndir = "D"\n',
                                          encoding="utf-8")
    res = run_push(_ns(data_dir=str(data_dir), dir=None))
    assert res.exit_code == 2
    assert any("未指定同步目录" in line for line in res.err_lines)
    assert any("[snyc]" in line and "sync" in line for line in res.err_lines)


def test_run_push_config_rejected_still_reports_push(tmp_path: Path) -> None:
    """Spec 6 CFG-1：落配置拒写时，推送成功事实仍报告，且退出码为错误。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    original = "[meta]\nlast_run = 2026-01-01\n"
    (data_dir / "config.toml").write_text(original, encoding="utf-8")

    res = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=False))
    assert res.exit_code == 1
    assert res.out_lines[0].startswith("已推送 0 个包共 0 字节")
    assert any("落配置失败" in line and "[sync]" in line
               for line in res.err_lines)
    assert (data_dir / "config.toml").read_text(encoding="utf-8") == original


def test_run_push_dry_run_never_writes_config(tmp_path: Path) -> None:
    """Spec 6 CFG-1：--dry-run 不触发写路径（锁定现状）。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    original = "[meta]\nlast_run = 2026-01-01\n"
    (data_dir / "config.toml").write_text(original, encoding="utf-8")

    res = run_push(_ns(data_dir=str(data_dir), dir=str(sync_dir), dry_run=True))
    assert res.exit_code == 0
    assert (data_dir / "config.toml").read_text(encoding="utf-8") == original


# ---------------- push/pull 加密模式（Spec 14 D4/D7） ----------------

def _push_built_package(tmp_path, memory_tree):
    """走完整链路备一个本机归档包：import → export --archive，返回 data_dir。"""
    data_dir = tmp_path / "data"
    assert main(["import", "--data-dir", str(data_dir),
                 "--memory-root", str(memory_tree)]) == 0
    conn = catalog.connect(data_dir / "catalog.sqlite")
    try:
        project_id = sorted(p["project_id"] for p in catalog.get_projects(conn))[0]
    finally:
        conn.close()
    assert main(["export", "--archive", f"--project={project_id}",
                 "--out", str(data_dir / "archives"),
                 "--data-dir", str(data_dir)]) == 0
    return data_dir


def test_push_encrypt_env_var_and_persist(tmp_path, monkeypatch, capsys,
                                          memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = main(["push", "--dir", str(share), "--encrypt",
                "--data-dir", str(data_dir)])
    assert res == 0
    envelopes = list(share.glob("spk-*.spk"))
    assert len(envelopes) == 1
    assert list((data_dir / "archives").glob("*.zip"))   # 本机仍是明文包
    # --encrypt 与 dir 一样在成功后持久化
    assert config.sync_encrypt(Path(data_dir)) is True


def test_push_encrypt_reports_undecryptable(tmp_path, monkeypatch, memory_tree):
    """B1：push 报告（文本 + json）透出无法解密信封数，不改重推行为。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    share.mkdir()
    (share / ("spk-" + "e" * 32 + ".spk")).write_bytes(b"garbage")
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = run_push(_ns(data_dir=str(data_dir), dir=str(share),
                       encrypt=True, json=True, dry_run=False))
    assert res.exit_code == 0
    assert res.data["pushed"] == 1
    assert res.data["skipped_undecryptable"] == 1
    assert "无法解密 1" in res.out_lines[0]
    # 再推一次：信封仍无法解密，本地包仍重推（行为不变）
    res2 = run_push(_ns(data_dir=str(data_dir), dir=str(share),
                        encrypt=True, json=True, dry_run=False))
    assert res2.exit_code == 0
    assert res2.data["skipped_undecryptable"] == 1


def _count_decrypts(monkeypatch):
    """给 crypto.decrypt_envelope 挂调用计数（sync/CLI 均按属性访问）."""
    calls = {"n": 0}
    real = crypto.decrypt_envelope

    def wrapper(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr("sessionpack.crypto.decrypt_envelope", wrapper)
    return calls


def test_push_encrypt_persists_envelope_cache_and_reuses(tmp_path, monkeypatch,
                                                         memory_tree):
    """B2：push 成功后在 data-dir 落信封清单；二次 push 命中清单零解密。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = run_push(_ns(data_dir=str(data_dir), dir=str(share),
                       encrypt=True, json=True, dry_run=False))
    assert res.exit_code == 0 and res.data["pushed"] == 1
    cache_file = data_dir / "envelope-cache.json"
    assert cache_file.is_file()
    import json as _json
    cache = _json.loads(cache_file.read_text("utf-8"))
    envelopes = list(share.glob("spk-*.spk"))
    assert set(cache) == {p.name for p in envelopes}
    calls = _count_decrypts(monkeypatch)
    res2 = run_push(_ns(data_dir=str(data_dir), dir=str(share),
                        encrypt=True, json=True, dry_run=False))
    assert res2.exit_code == 0
    assert res2.data["pushed"] == 0
    assert res2.data["skipped_existing"] == 1
    assert calls["n"] == 0          # 判重全程走清单，无 PBKDF2


def test_push_dry_run_and_plain_do_not_touch_cache(tmp_path, monkeypatch,
                                                   memory_tree):
    """dry-run 与非加密 push 都不落清单（缓存只随实际写入演进）。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    run_push(_ns(data_dir=str(data_dir), dir=str(share),
                 encrypt=True, json=True, dry_run=True))
    assert not (data_dir / "envelope-cache.json").exists()
    run_push(_ns(data_dir=str(data_dir), dir=str(tmp_path / "plain"),
                 encrypt=False, json=True, dry_run=False))
    assert not (data_dir / "envelope-cache.json").exists()


def test_pull_encrypt_reuses_envelope_cache(tmp_path, monkeypatch, memory_tree):
    """B2：pull 二次幂等重跑命中清单 → 判重零解密，计数不变。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    assert main(["push", "--dir", str(share), "--encrypt",
                 "--data-dir", str(data_dir)]) == 0
    data_b = tmp_path / "data-b"
    assert main(["import", "--data-dir", str(data_b),
                 "--memory-root", str(memory_tree)]) == 0
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    res1 = run_pull(_ns(data_dir=str(data_b), dir=str(share), json=False))
    assert res1.exit_code == 0
    cache_file = data_b / "envelope-cache.json"
    assert cache_file.is_file()    # pull 同样持久化共享清单
    calls = _count_decrypts(monkeypatch)
    res2 = run_pull(_ns(data_dir=str(data_b), dir=str(share), json=False))
    assert res2.exit_code == 0
    assert calls["n"] == 0         # 二轮判重零解密


def test_broken_envelope_cache_is_ignored(tmp_path, monkeypatch, memory_tree):
    """清单文件损坏（手工编辑/半写）→ 静默回退无缓存路径，push 照常成功。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    (data_dir / "envelope-cache.json").write_text("{not json", "utf-8")
    res = run_push(_ns(data_dir=str(data_dir), dir=str(share),
                       encrypt=True, json=True, dry_run=False))
    assert res.exit_code == 0
    assert res.data["pushed"] == 1


def test_push_encrypt_dry_run_env_var_no_write(tmp_path, monkeypatch, memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = main(["push", "--dir", str(share), "--encrypt", "--dry-run",
                "--data-dir", str(data_dir)])
    assert res == 0
    assert not share.exists()                        # dry-run 不落目录不写文件
    assert config.sync_encrypt(Path(data_dir)) is False  # dry-run 不持久化


def test_push_encrypt_no_passphrase_usage(tmp_path, monkeypatch, capsys,
                                          memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    monkeypatch.delenv("SESSIONPACK_PASSPHRASE", raising=False)
    monkeypatch.setattr("sessionpack.cli.write_cmds.getpass.getpass",
                        lambda prompt="": "")       # 交互空口令 → 拒绝
    res = main(["push", "--dir", str(tmp_path / "share"), "--encrypt",
                "--data-dir", str(data_dir)])
    assert res == 2
    assert "口令" in capsys.readouterr().err


def test_push_no_encrypt_explicit_overrides_config(tmp_path, monkeypatch, capsys,
                                                   memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    config.set_sync_encrypt(Path(data_dir), True)    # 配置里开着加密
    share = tmp_path / "share"
    res = main(["push", "--dir", str(share), "--no-encrypt",
                "--data-dir", str(data_dir)])
    assert res == 0
    assert list(share.glob("*.zip"))                 # 显式覆盖后推的是明文
    assert not list(share.glob("*.spk"))
    assert config.sync_encrypt(Path(data_dir)) is False  # 覆盖同时持久化


def test_pull_encrypt_env_var(tmp_path, monkeypatch, memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    assert main(["push", "--dir", str(share), "--encrypt",
                 "--data-dir", str(data_dir)]) == 0
    data_b = tmp_path / "data-b"
    assert main(["import", "--data-dir", str(data_b),
                 "--memory-root", str(memory_tree)]) == 0
    # B 机器清空 archives，只靠 pull 回灌
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_b)]) == 0
    assert list((data_b / "archives").glob("*.zip"))  # 落的是规范包名
    assert not list((data_b / "archives").glob("*.spk"))


def test_pull_plain_dir_ignores_env_var(tmp_path, monkeypatch, memory_tree):
    """pull 只看文件后缀自适应：明文目录有环境变量也不询问、不报错（D7）。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0
    monkeypatch.delenv("SESSIONPACK_PASSPHRASE", raising=False)
    data_b = tmp_path / "data-b"
    assert main(["import", "--data-dir", str(data_b),
                 "--memory-root", str(memory_tree)]) == 0
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_b)]) == 0
    assert list((data_b / "archives").glob("*.zip"))


def test_pull_nonstandard_spk_does_not_trigger_passphrase(tmp_path, monkeypatch,
                                                          memory_tree):
    """非规范名 .spk（缺 `spk-` 前缀）不触发口令采集，明文 pull 照常（Spec 14 修复③）。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0   # 明文推送
    (share / "notes.spk").write_bytes(b"not an envelope")
    monkeypatch.delenv("SESSIONPACK_PASSPHRASE", raising=False)
    monkeypatch.setattr("sessionpack.cli.write_cmds.getpass.getpass",
                        lambda prompt="": "")          # 一旦误采集口令 → 退出码 2
    data_b = tmp_path / "data-b"
    assert main(["import", "--data-dir", str(data_b),
                 "--memory-root", str(memory_tree)]) == 0
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_b)]) == 0
    assert list((data_b / "archives").glob("*.zip"))


def test_run_reencrypt_unlink_failure_text(tmp_path, monkeypatch,
                                           memory_tree):
    """信封 os.replace 就位后原 zip 删除失败：记 errors 且注明信封已就位，不吞栈。"""
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0
    plain = sorted(p for p in share.glob("*.zip")
                   if not p.name.startswith("."))
    assert plain
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    real_unlink = Path.unlink

    def boom(self, *args, **kwargs):
        if self.suffix == ".zip":
            raise OSError("原包被占用")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", boom)
    res = run_reencrypt(_ns(data_dir=str(data_dir), dir=str(share),
                            json=False))
    assert res.exit_code == 1
    assert len(res.data["errors"]) == 1
    assert "信封已就位，原明文包删除失败" in res.data["errors"][0]
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert list(share.glob("spk-*.spk"))               # 信封已就位
    assert sorted(p for p in share.glob("*.zip")
                  if not p.name.startswith(".")) == plain  # 原包保留可重试


def test_run_pull_requires_existing_dir(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    missing = tmp_path / "sync"

    res = run_pull(_ns(data_dir=str(data_dir), dir=str(missing)))
    assert res.exit_code == 2
    assert res.err_lines == [f"同步目录不存在: {missing}"]

    missing.mkdir()
    ok = run_pull(_ns(data_dir=str(data_dir), dir=str(missing)))
    assert ok.exit_code == 0
    assert ok.out_lines == ["已拉取 0 个包（新增会话 0、消息 0），"
                            "跳过已存在 0，跳过无效包 0"]


def test_run_import_rejects_future_version_db(tmp_path: Path) -> None:
    """Spec 7 D1：库比工具新 → EXIT_ERROR + 带版本号文案，不崩栈。"""
    data_dir = _data_dir(tmp_path)
    conn = catalog.connect(data_dir / "catalog.sqlite")
    catalog.init_schema(conn)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    zip_path = tmp_path / "whatever.zip"
    zip_path.write_bytes(b"")
    res = run_import(_ns(memory_root=str(tmp_path / "nope"),
                         data_dir=str(data_dir), archive=str(zip_path),
                         project=None, since=None, until=None, reparse=False,
                         retry_failed=False, audit=False))
    assert res.exit_code == 1
    assert "99" in res.err_lines[0]


def test_run_pull_rejects_legacy_db(tmp_path: Path) -> None:
    """Spec 7 D1：pull 遇缺列史前库 → EXIT_ERROR + 重建导向文案。"""
    data_dir = _data_dir(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    conn = catalog.connect(data_dir / "catalog.sqlite")
    conn.executescript(
        "CREATE TABLE sessions ("
        "session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
        "source_kind TEXT NOT NULL, title TEXT, summary TEXT,"
        "created_at TEXT, updated_at TEXT, status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.commit()
    conn.close()
    res = run_pull(_ns(data_dir=str(data_dir), dir=str(sync_dir)))
    assert res.exit_code == 1
    assert "content_hash" in res.err_lines[0]
    assert "rebuild --from-archives" in res.err_lines[0]


def _seed_doctor_db(tmp_path: Path) -> Path:
    data_dir = _data_dir(tmp_path)
    conn = catalog.connect(data_dir / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_message(conn, MessageRecord(
        session_id="a" * 24, message_id="m1", seq=1, ts="2026-01-01 10:00:00",
        intent="导入会话", outcome="一致的正文", actions=(), learned=(),
        meta=None, body="一致的正文", project_id="-d-Work-RepoA",
        source_kind="trae-memory"))
    conn.commit()
    conn.close()
    return data_dir


def test_run_doctor_reports_reconcile_consistent(tmp_path: Path) -> None:
    """Spec 7 D2：一致库 → 对账行「一致」、无索引警告、退出码 0。"""
    data_dir = _seed_doctor_db(tmp_path)
    res = run_doctor(_ns(data_dir=str(data_dir), json=False))
    reconcile = [ln for ln in res.out_lines if ln.startswith("索引对账")]
    assert len(reconcile) == 1
    assert "一致" in reconcile[0]
    assert not [ln for ln in res.out_lines if ln.startswith("索引警告")]
    assert res.exit_code == 0
    payload = run_doctor(_ns(data_dir=str(data_dir), json=True)).data
    assert payload["catalog"]["fts"]["drift"] is False
    assert payload["catalog"]["version"] == 1


def test_run_doctor_flags_fts_drift(tmp_path: Path) -> None:
    """Spec 7 D2：手删 FTS 一行 → 对账行漂移 + 索引警告 + warnings 非空。"""
    data_dir = _seed_doctor_db(tmp_path)
    conn = catalog.connect(data_dir / "catalog.sqlite")
    conn.execute("DELETE FROM message_fts")
    conn.commit()
    conn.close()
    res = run_doctor(_ns(data_dir=str(data_dir), json=False))
    reconcile = [ln for ln in res.out_lines if ln.startswith("索引对账")]
    assert "漂移" in reconcile[0]
    warnings = [ln for ln in res.out_lines if ln.startswith("索引警告")]
    assert warnings and "rebuild --fts" in warnings[0]
    payload = run_doctor(_ns(data_dir=str(data_dir), json=True)).data
    assert payload["warnings"]
    assert payload["catalog"]["fts"]["drift"] is True


def test_parser_still_builds_every_subcommand() -> None:
    """删除 set_defaults(func=...) 后，参数面必须与拆分前完全一致。"""
    args = build_parser().parse_args(["export", "--all", "--out", "x.zip",
                                      "--archive"])
    assert args.command == "export"
    assert args.all is True and args.out == "x.zip" and args.archive is True


def test_serve_guard_without_catalog_is_usage_error(tmp_path: Path,
                                                    capsys) -> None:
    """serve 特例：不产出 CommandResult，直接返回退出码。"""
    data_dir = _data_dir(tmp_path)
    code = main(["serve", "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == "索引不存在，请先运行 sessionpack import\n"


# ---------- Spec 7 D3：rebuild --fts / --from-archives ----------


def _seed_rebuild_db(tmp_path: Path) -> Path:
    """1 项目 1 会话 2 条含「冲锋衣」的消息，返回 data_dir。"""
    data_dir = _data_dir(tmp_path)
    conn = catalog.connect(data_dir / "catalog.sqlite")
    catalog.init_schema(conn)
    catalog.upsert_project(conn, ProjectRecord(
        project_id="-d-Work-RepoA", display_name="RepoA",
        local_path_hint="", source_kind="trae-memory"))
    for mid, seq in (("m1", 1), ("m2", 2)):
        catalog.upsert_message(conn, MessageRecord(
            session_id="a" * 24, message_id=mid, seq=seq,
            ts="2026-01-01 10:00:00", intent="导入会话", outcome="冲锋衣选购",
            actions=(), learned=(), meta=None, body="冲锋衣选购建议",
            project_id="-d-Work-RepoA", source_kind="trae-memory"))
    conn.commit()
    conn.close()
    return data_dir


def _drop_fts_row(data_dir: Path, message_id: str) -> None:
    conn = catalog.connect(data_dir / "catalog.sqlite")
    conn.execute("DELETE FROM message_fts WHERE message_id=?", (message_id,))
    conn.commit()
    conn.close()


def test_run_rebuild_fts_restores_search_after_drift(tmp_path: Path) -> None:
    """§4 验收：漂移库 --fts 后 search_messages('冲锋衣') 恢复命中、对账一致。"""
    data_dir = _seed_rebuild_db(tmp_path)
    _drop_fts_row(data_dir, "m2")
    conn = catalog.connect(data_dir / "catalog.sqlite")
    assert len(catalog.search_messages(conn, "冲锋衣")) == 1
    conn.close()

    res = run_rebuild(_ns(data_dir=str(data_dir), fts=True))
    assert res.exit_code == 0
    assert res.out_lines == ["FTS 重建完成：2 条"]

    conn = catalog.connect(data_dir / "catalog.sqlite")
    try:
        assert len(catalog.search_messages(conn, "冲锋衣")) == 2
        from sessionpack.catalog.read import reconcile_fts
        assert reconcile_fts(conn)["drift"] is False
    finally:
        conn.close()


def test_run_rebuild_fts_is_idempotent(tmp_path: Path) -> None:
    """§4 验收：幂等——连跑两次结果一致，行数不翻倍。"""
    data_dir = _seed_rebuild_db(tmp_path)
    first = run_rebuild(_ns(data_dir=str(data_dir)))  # --fts 是缺省模式
    second = run_rebuild(_ns(data_dir=str(data_dir), fts=True))
    assert first.exit_code == second.exit_code == 0
    assert first.out_lines == second.out_lines == ["FTS 重建完成：2 条"]
    conn = catalog.connect(data_dir / "catalog.sqlite")
    try:
        assert catalog.stats(conn)["fts_rows"] == 2
    finally:
        conn.close()


def test_run_rebuild_fts_missing_catalog_is_usage_error(tmp_path: Path) -> None:
    """§4 验收：库不存在 → EXIT_USAGE，且不隐式建库。"""
    data_dir = _data_dir(tmp_path)
    res = run_rebuild(_ns(data_dir=str(data_dir), fts=True))
    assert res.exit_code == 2
    assert res.err_lines == ["索引不存在，请先运行 sessionpack import"]
    assert res.data is None


def test_run_rebuild_fts_json_payload(tmp_path: Path) -> None:
    """§4 CLI 契约：json 模式返回 {"rebuilt": n, "fts": {…对账…}}。"""
    data_dir = _seed_rebuild_db(tmp_path)
    _drop_fts_row(data_dir, "m1")
    res = run_rebuild(_ns(data_dir=str(data_dir), fts=True, json=True))
    assert res.exit_code == 0
    assert res.out_lines == []
    assert res.data["rebuilt"] == 2
    assert res.data["fts"]["drift"] is False
    assert res.data["fts"]["fts_rows"] == 2


def _build_project_archive(data_dir: Path) -> archive.ArchiveResult:
    conn = catalog.connect(data_dir / "catalog.sqlite")
    try:
        return archive.build_archive(conn, data_dir / "archives",
                                     "-d-Work-RepoA")
    finally:
        conn.close()


def _corrupt_zip(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("messages.ndjson"):
                data = b"corrupted\n"
            zout.writestr(item, data)


def test_run_rebuild_from_archives_requires_yes(tmp_path: Path) -> None:
    """§5 验收：缺 --yes 拒绝执行（EXIT_USAGE 并说明后果）。"""
    data_dir = _seed_rebuild_db(tmp_path)
    res = run_rebuild(_ns(data_dir=str(data_dir), from_archives=True,
                          yes=False))
    assert res.exit_code == 2
    assert "--yes" in res.err_lines[0]
    assert "丢弃" in res.err_lines[0]


def test_run_rebuild_from_archives_without_archives(tmp_path: Path) -> None:
    """§5 验收：无归档包 → 失败并给出 import 指引。"""
    data_dir = _seed_rebuild_db(tmp_path)
    res = run_rebuild(_ns(data_dir=str(data_dir), from_archives=True,
                          yes=True))
    assert res.exit_code == 1
    assert "无归档包" in res.err_lines[0]
    assert "sessionpack import --memory-root" in res.err_lines[0]


def test_run_rebuild_from_archives_success_with_backup(tmp_path: Path) -> None:
    """§5 验收：正常重建后 stats 与源一致、.bak 存在、搜索恢复命中。"""
    data_dir = _seed_rebuild_db(tmp_path)
    _build_project_archive(data_dir)
    _drop_fts_row(data_dir, "m1")  # 顺带验证 staging 自检兜底后交付一致库

    res = run_rebuild(_ns(data_dir=str(data_dir), from_archives=True,
                          yes=True, json=True))
    assert res.exit_code == 0
    assert res.data["ok"] is True
    assert res.data["sessions"] == 1
    assert res.data["messages"] == 2
    assert res.data["fts_rows"] == 2
    assert len(res.data["packages"]) == 1
    backup = Path(res.data["backup"])
    assert backup.name == "catalog.sqlite.bak"
    assert backup.is_file()

    conn = catalog.connect(data_dir / "catalog.sqlite")
    try:
        assert len(catalog.search_messages(conn, "冲锋衣")) == 2
    finally:
        conn.close()
    assert not list(data_dir.glob("catalog.sqlite.staging*"))


def test_run_rebuild_from_archives_bad_zip_fail_closed(tmp_path: Path) -> None:
    """§5 验收：任一 zip 校验 BAD → 中止，原库字节不变、无 staging 残留。"""
    data_dir = _seed_rebuild_db(tmp_path)
    built = _build_project_archive(data_dir)
    _corrupt_zip(built.path, data_dir / "archives" / "bad.zip")
    before = (data_dir / "catalog.sqlite").read_bytes()

    res = run_rebuild(_ns(data_dir=str(data_dir), from_archives=True,
                          yes=True))
    assert res.exit_code == 1
    assert "bad.zip" in res.err_lines[0]
    assert "原索引未改动" in res.err_lines[0]
    assert (data_dir / "catalog.sqlite").read_bytes() == before
    assert not list(data_dir.glob("catalog.sqlite.staging*"))
    assert not (data_dir / "catalog.sqlite.bak").exists()


# ---------------- reencrypt 迁移（Spec 14 D8） ----------------

def test_reencrypt_converts_and_roundtrips(tmp_path, monkeypatch, memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0
    zips = list(share.glob("*.zip"))
    assert len(zips) == 1
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = main(["reencrypt", "--dir", str(share), "--data-dir", str(data_dir)])
    assert res == 0
    assert not list(share.glob("*.zip"))             # 原 zip 已删
    assert len(list(share.glob("spk-*.spk"))) == 1   # 信封落位
    # 迁移后 pull 链路照常（口令同一条）
    data_b = tmp_path / "data-b"
    assert main(["import", "--data-dir", str(data_b),
                 "--memory-root", str(memory_tree)]) == 0
    for p in (data_b / "archives").glob("*.zip"):
        p.unlink()
    assert main(["pull", "--dir", str(share), "--data-dir", str(data_b)]) == 0
    assert list((data_b / "archives").glob("*.zip"))


def test_reencrypt_skips_envelopes_and_bad_verify(tmp_path, monkeypatch,
                                                  memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0
    good = sorted(p.name for p in share.glob("*.zip"))
    assert len(good) == 1
    (share / ("spk-" + "0" * 32 + ".spk")).write_bytes(b"already envelope")
    (share / "broken.zip").write_bytes(b"not a zip")
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "s3cret-pass")
    res = main(["reencrypt", "--dir", str(share), "--json",
                "--data-dir", str(data_dir)])
    assert res == 0
    # _run_cli 的 json 渲染不进 capsys 返回值断言路径，直接查目录状态
    assert not (share / good[0]).exists()            # 好包已转成信封并删除
    assert (share / "broken.zip").exists()           # 校验未通过：只跳过，原包保留
    assert len(list(share.glob("spk-*.spk"))) == 2   # 原有假信封 + 新信封


def test_reencrypt_missing_dir_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONPACK_PASSPHRASE", "x")
    res = main(["reencrypt", "--dir", str(tmp_path / "nope"),
                "--data-dir", str(tmp_path / "data")])
    assert res == 2


def test_reencrypt_empty_passphrase_usage(tmp_path, monkeypatch, memory_tree):
    data_dir = _push_built_package(tmp_path, memory_tree)
    share = tmp_path / "share"
    assert main(["push", "--dir", str(share),
                 "--data-dir", str(data_dir)]) == 0
    monkeypatch.delenv("SESSIONPACK_PASSPHRASE", raising=False)
    monkeypatch.setattr("sessionpack.cli.write_cmds.getpass.getpass",
                        lambda prompt="": "")
    res = main(["reencrypt", "--dir", str(share), "--data-dir", str(data_dir)])
    assert res == 2
