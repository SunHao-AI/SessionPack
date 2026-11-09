"""Spec 14：信封原语的往返、防篡改与口令错误路径。"""
from __future__ import annotations

import pytest

from sessionpack import crypto


def test_roundtrip_returns_exact_bytes() -> None:
    data = "会话正文：包含中文与 emoji ✅".encode()
    name = crypto.new_envelope_name()
    blob = crypto.encrypt_envelope(data, "correct horse", name)
    assert crypto.decrypt_envelope(blob, "correct horse", name) == data


def test_envelope_header_layout() -> None:
    name = "spk-" + "0" * 32 + ".spk"
    blob = crypto.encrypt_envelope(b"x" * 100, "p", name)
    assert blob[:5] == b"SPKE1"          # magic
    assert blob[5] == 1                   # 版本
    assert int.from_bytes(blob[22:26], "big") == crypto.PBKDF2_ITERATIONS
    assert len(blob) > 48 + 100           # 头 48B + 密文 + 16B tag


def test_iterations_recorded_in_header() -> None:
    """迭代次数写进信封头：日后升档，旧信封仍可解（spec §4 D2）。"""
    name = crypto.new_envelope_name()
    blob = crypto.encrypt_envelope(b"payload", "p", name)
    assert int.from_bytes(blob[22:26], "big") == 600_000


def test_wrong_passphrase_raises_envelope_error() -> None:
    name = crypto.new_envelope_name()
    blob = crypto.encrypt_envelope(b"payload", "right", name)
    with pytest.raises(crypto.EnvelopeError) as exc:
        crypto.decrypt_envelope(blob, "wrong", name)
    assert "E201" in str(exc.value)
    assert "payload" not in str(exc.value)


def test_tampered_ciphertext_raises_envelope_error() -> None:
    name = crypto.new_envelope_name()
    blob = bytearray(crypto.encrypt_envelope(b"payload", "p", name))
    blob[-1] ^= 0x01                      # 翻转 tag 的末位
    with pytest.raises(crypto.EnvelopeError):
        crypto.decrypt_envelope(bytes(blob), "p", name)


def test_wrong_envelope_name_raises_envelope_error() -> None:
    """AAD 绑定文件名：把信封整体改名后应解不开（防冒名顶替）。"""
    real = crypto.new_envelope_name()
    other = crypto.new_envelope_name()
    blob = crypto.encrypt_envelope(b"payload", "p", real)
    with pytest.raises(crypto.EnvelopeError):
        crypto.decrypt_envelope(blob, "p", other)


def test_bad_magic_and_truncated_raise_e202() -> None:
    with pytest.raises(crypto.EnvelopeError) as exc:
        crypto.decrypt_envelope(b"ZIP" + b"\x00" * 64, "p", "spk-a.spk")
    assert "E202" in str(exc.value)
    with pytest.raises(crypto.EnvelopeError) as exc:
        crypto.decrypt_envelope(b"SPKE1\x01", "p", "spk-a.spk")
    assert "E202" in str(exc.value)


def test_unsupported_version_raises_e202() -> None:
    name = crypto.new_envelope_name()
    blob = bytearray(crypto.encrypt_envelope(b"payload", "p", name))
    blob[5] = 2
    with pytest.raises(crypto.EnvelopeError) as exc:
        crypto.decrypt_envelope(bytes(blob), "p", name)
    assert "E202" in str(exc.value)


def test_name_helpers() -> None:
    name = crypto.new_envelope_name()
    assert name.startswith("spk-") and name.endswith(".spk")
    assert len(name) == 4 + 32 + 4
    assert crypto.is_envelope_name(name)
    assert not crypto.is_envelope_name("proj-a-2026-11-06-0123456789ab.zip")


def test_derive_key_is_deterministic_and_salted() -> None:
    k1 = crypto.derive_key("p", b"0" * 16, 600_000)
    k2 = crypto.derive_key("p", b"0" * 16, 600_000)
    k3 = crypto.derive_key("p", b"1" * 16, 600_000)
    assert k1 == k2 and len(k1) == 32
    assert k1 != k3
