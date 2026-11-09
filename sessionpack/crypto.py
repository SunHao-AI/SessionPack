"""同步目录端到端加密（Spec 14 D1-D4）：全项目唯一允许 import cryptography 的文件。

信封格式（版本 0x01，头长 38B）：
    magic "SPKE1"(5B) | 版本 0x01(1B) | salt(16B) | iterations(uint32 大端 4B)
    | nonce(12B) | ciphertext(含 AESGCM 16B 认证 tag)
KDF 为 PBKDF2-HMAC-SHA256；iterations 写入信封头，日后升档旧信封仍可解。
AAD 取信封文件名的字节：防止密文在文件之间整体搬运、冒名顶替。
本层是纯函数，不弹窗、不读环境变量、不碰磁盘——口令一律由调用方传入。
"""

from __future__ import annotations

import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"SPKE1"
VERSION = 1
KDF_SALT_LEN = 16
NONCE_LEN = 12
PBKDF2_ITERATIONS = 600_000
# iterations 来自信封头，属外部可控字段：设上限封死解密侧的无界 KDF 开销。
# 上限取当前档位 600_000 的约 8 倍，够日后升档，又不至于让对端花近乎无限的时间派生密钥。
PBKDF2_ITERATIONS_MIN = 1
PBKDF2_ITERATIONS_MAX = 5_000_000
_HEADER_LEN = len(MAGIC) + 1 + KDF_SALT_LEN + 4 + NONCE_LEN   # 38

NAME_PREFIX = "spk-"
NAME_SUFFIX = ".spk"


class EnvelopeError(ValueError):
    """信封非法：magic/版本/长度不对、口令错误或认证失败。消息含错误码不含明文。"""


def derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 派生 32 字节密钥；iterations 由调用方从信封头读出。"""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def new_envelope_name() -> str:
    """随机信封名（与内容无关）：spk-<32hex>.spk。"""
    return f"{NAME_PREFIX}{secrets.token_hex(16)}{NAME_SUFFIX}"


def is_envelope_name(name: str) -> bool:
    """宽松判定：前缀 spk- 且后缀 .spk 即视为信封（结构问题留给解密阶段）。"""
    return name.startswith(NAME_PREFIX) and name.endswith(NAME_SUFFIX)


def encrypt_envelope(data: bytes, passphrase: str, envelope_name: str) -> bytes:
    """加密为信封字节流；envelope_name 的字节作 AAD 参与认证。"""
    salt = os.urandom(KDF_SALT_LEN)
    key = derive_key(passphrase, salt, PBKDF2_ITERATIONS)
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, data, envelope_name.encode("utf-8"))
    return (MAGIC + bytes([VERSION]) + salt
            + PBKDF2_ITERATIONS.to_bytes(4, "big") + nonce + ct)


def decrypt_envelope(blob: bytes, passphrase: str, envelope_name: str) -> bytes:
    """解密信封字节流；一切失败统一抛 EnvelopeError（E202 结构 / E201 口令）。"""
    if len(blob) < _HEADER_LEN:
        raise EnvelopeError("E202: 信封长度不足")
    if blob[:len(MAGIC)] != MAGIC:
        raise EnvelopeError("E202: magic 不匹配")
    if blob[len(MAGIC)] != VERSION:
        raise EnvelopeError(f"E202: 不支持的信封版本 {blob[len(MAGIC)]}")
    salt = blob[6:6 + KDF_SALT_LEN]
    iterations = int.from_bytes(blob[22:26], "big")
    if not PBKDF2_ITERATIONS_MIN <= iterations <= PBKDF2_ITERATIONS_MAX:
        raise EnvelopeError(f"E202: iterations 越界 {iterations}")
    nonce = blob[26:_HEADER_LEN]
    ct = blob[_HEADER_LEN:]
    key = derive_key(passphrase, salt, iterations)
    try:
        return AESGCM(key).decrypt(nonce, ct, envelope_name.encode("utf-8"))
    except InvalidTag as exc:
        raise EnvelopeError("E201: 口令错误或信封被篡改") from exc
