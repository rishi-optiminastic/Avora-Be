"""Encryption for Access secrets - the password or API key stored against a
tech asset (Security rule 5.6).

Keys come from `Settings.access_crypto_key` (the dedicated `ACCESS_FERNET_KEY`,
else `ENVSYNC_FERNET_KEY`). Plaintext exists only inside a reveal or a write;
it is never logged, never put in an audit row and never in a list response.
"""

from __future__ import annotations

from cryptography.fernet import InvalidToken, MultiFernet

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.core.fernet_keys import KeyConfigError, multi_fernet

_SETTING = "ACCESS_FERNET_KEY (or ENVSYNC_FERNET_KEY)"


def _cipher(settings: Settings) -> MultiFernet:
    try:
        return multi_fernet(settings.access_crypto_key, setting=_SETTING)
    except KeyConfigError as exc:
        raise ValidationError("Secret storage is not configured on this server.") from exc


def encrypt_secret(settings: Settings, plaintext: str) -> str:
    return _cipher(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(settings: Settings, ciphertext: str) -> str:
    try:
        return _cipher(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Written under a key that is no longer configured. Say so, never echo it.
        raise ValidationError("This secret was stored under a retired key; rotate it.") from exc
