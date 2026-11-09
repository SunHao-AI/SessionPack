"""E_CRYPTO：cryptography 只许 crypto.py 用（Spec 14 D8）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint_imports import LAYER_OF, check_source

_SRC = "from cryptography.exceptions import InvalidTag\n"


def test_crypto_is_layer_zero() -> None:
    assert LAYER_OF["crypto"] == 0


@pytest.mark.parametrize("src", [
    _SRC,
    "import cryptography\n",
    "import cryptography.hazmat.x\n",
    "from cryptography import x\n",
])
def test_cryptography_blocked_spellings_outside_crypto(src: str) -> None:
    """三种 import 拼写在 crypto.py 之外一律 E_CRYPTO（含裸 import）。"""
    out = "\n".join(check_source(src, "sessionpack/sync.py"))
    assert "E_CRYPTO" in out


def test_cryptography_allowed_only_in_crypto_layer(tmp_path: Path) -> None:
    bad = tmp_path / "sessionpack" / "sync.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(_SRC, encoding="utf-8")
    out = "\n".join(check_source(bad.read_text(encoding="utf-8"),
                                 "sessionpack/sync.py"))
    assert "E_CRYPTO" in out

    good = tmp_path / "sessionpack" / "crypto.py"
    good.write_text(_SRC, encoding="utf-8")
    assert not any("E_CRYPTO" in line for line in
                   check_source(good.read_text(encoding="utf-8"),
                                "sessionpack/crypto.py"))
