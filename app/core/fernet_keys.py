"""One way to turn a configured key string into a cipher.

Every at-rest secret in Avora (bank details, Access passwords) is Fernet
encrypted under keys read from settings. The string may hold several keys,
whitespace or comma separated, newest first, so a `MultiFernet` keeps reading
rows written under an older key after a rotation.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet


class KeyConfigError(RuntimeError):
    """Raised when a key is missing or malformed. The message names the setting,
    never the key material."""


def multi_fernet(raw: str, *, setting: str) -> MultiFernet:
    keys = [k.strip() for k in raw.replace(",", " ").split() if k.strip()]
    if not keys:
        raise KeyConfigError(f"No encryption key configured. Set {setting} to a Fernet key.")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:  # malformed key material
        raise KeyConfigError(f"The key in {setting} is malformed.") from exc
