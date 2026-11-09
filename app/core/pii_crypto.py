"""Symmetric encryption for sensitive employee PII at rest — currently bank
account numbers (Fernet = AES-128-CBC + HMAC), Security rule 5.6.

The key comes from `Settings.bank_crypto_key` (the dedicated `bank_fernet_key`,
falling back to `envsync_fernet_key`) — env only, never hardcoded, never logged.
Like Env Sync it accepts several whitespace/comma-separated keys (newest first)
so a `MultiFernet` keeps decrypting old rows after a key rotation.
"""

from __future__ import annotations

from cryptography.fernet import MultiFernet

from app.core.config import Settings
from app.core.fernet_keys import KeyConfigError, multi_fernet


class PiiCryptoError(RuntimeError):
    """Raised when the PII encryption key is missing or malformed."""


def _cipher(settings: Settings) -> MultiFernet:
    try:
        return multi_fernet(
            settings.bank_crypto_key, setting="BANK_FERNET_KEY (or ENVSYNC_FERNET_KEY)"
        )
    except KeyConfigError as exc:
        raise PiiCryptoError(str(exc)) from exc


def encrypt_pii(settings: Settings, plaintext: str) -> str:
    return _cipher(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_pii(settings: Settings, ciphertext: str) -> str:
    return _cipher(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
