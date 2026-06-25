"""Symmetric encryption for Env Sync secret content at rest (Fernet =
AES-128-CBC + HMAC).

The key comes from `Settings.envsync_fernet_key` (env only — never hardcoded,
never in the DB, never logged; Security rule 5.6). Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

`envsync_fernet_key` may hold several whitespace/comma-separated keys (newest
first) — we build a `MultiFernet` so old rows still decrypt after a rotation
while new writes use the first key.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, MultiFernet

from app.core.config import Settings


class EnvCryptoError(RuntimeError):
    """Raised when the Env Sync encryption key is missing or malformed."""


def content_hash(plaintext: str) -> str:
    """SHA-256 of the plaintext — must match the extension's hashContent()."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _cipher(settings: Settings) -> MultiFernet:
    raw = settings.envsync_fernet_key.strip()
    if not raw:
        raise EnvCryptoError(
            "ENVSYNC_FERNET_KEY is not set. Generate one with "
            "Fernet.generate_key() and load it from your secret manager."
        )
    keys = [k.strip() for k in raw.replace(",", " ").split() if k.strip()]
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:  # malformed key material
        raise EnvCryptoError("ENVSYNC_FERNET_KEY is malformed.") from exc


def encrypt(settings: Settings, plaintext: str) -> str:
    return _cipher(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(settings: Settings, ciphertext: str) -> str:
    return _cipher(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
