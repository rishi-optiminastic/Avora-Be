"""Unit tests for the security primitives (pure functions)."""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings
from app.core.security import (
    TokenError,
    create_session_jwt,
    decode_better_auth_jwt,
    decode_session_jwt,
    hash_device_token,
    verify_device_token,
    verify_hmac_sha256,
)

# EdDSA keypair standing in for Better Auth's JWKS in unit tests.
_PRIV = Ed25519PrivateKey.generate()
_PRIVATE_PEM = _PRIV.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = (
    _PRIV.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        jwt_secret="unit-test-secret",
        jwt_issuer="https://accounts.google.com",
        jwt_audience="unit-aud",
        jwt_algorithm="HS256",
        better_auth_issuer="https://auth.test",
        better_auth_audience="pms-unit-aud",
        better_auth_jwt_algorithm="EdDSA",
        agent_token_pepper="unit-pepper",
        hr_webhook_secret="unit-hr-secret",
    )


def _ba_token(settings: Settings, **overrides: object) -> str:
    payload: dict[str, object] = {
        "sub": "ba-user-123",
        "email": "user@corp.test",
        "iss": settings.better_auth_issuer,
        "aud": settings.better_auth_audience,
        "exp": 9999999999,
    }
    payload.update(overrides)
    return jwt.encode(payload, _PRIVATE_PEM, algorithm="EdDSA")


def test_better_auth_jwt_roundtrip(settings: Settings) -> None:
    token = _ba_token(settings)
    claims = decode_better_auth_jwt(settings, token, key=_PUBLIC_PEM)
    assert claims["sub"] == "ba-user-123"
    assert claims["email"] == "user@corp.test"


def test_better_auth_wrong_audience_is_rejected(settings: Settings) -> None:
    token = _ba_token(settings, aud="someone-else")
    with pytest.raises(TokenError):
        decode_better_auth_jwt(settings, token, key=_PUBLIC_PEM)


def test_better_auth_hs_downgrade_is_rejected(settings: Settings) -> None:
    # A token forged with HS256 must never validate: the verifier passes an
    # EdDSA-only allowlist, so PyJWT rejects the HS-signed token by algorithm
    # before any signature check (defends the classic alg-confusion attack).
    forged = jwt.encode(
        {
            "sub": "x",
            "email": "x@corp.test",
            "iss": settings.better_auth_issuer,
            "aud": settings.better_auth_audience,
            "exp": 9999999999,
        },
        "attacker-chosen-secret",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_better_auth_jwt(settings, forged, key=_PUBLIC_PEM)


def test_better_auth_alg_none_is_rejected(settings: Settings) -> None:
    forged = jwt.encode({"sub": "x"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        decode_better_auth_jwt(settings, forged, key=_PUBLIC_PEM)


def test_jwt_roundtrip(settings: Settings) -> None:
    token = create_session_jwt(settings, subject="hr-123")
    claims = decode_session_jwt(settings, token)
    assert claims["sub"] == "hr-123"
    assert claims["iss"] == settings.jwt_issuer


def test_jwt_alg_none_is_rejected(settings: Settings) -> None:
    # Forge an unsigned token; it must not be accepted.
    forged = jwt.encode({"sub": "x"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        decode_session_jwt(settings, forged)


def test_jwt_wrong_audience_is_rejected(settings: Settings) -> None:
    bad = jwt.encode(
        {"sub": "x", "iss": settings.jwt_issuer, "aud": "someone-else", "exp": 9999999999},
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_session_jwt(settings, bad)


def test_device_token_hash_is_verifiable(settings: Settings) -> None:
    raw = "device-token-abc"
    stored = hash_device_token(settings, raw)
    assert verify_device_token(settings, raw, stored)
    assert not verify_device_token(settings, "wrong", stored)


def test_hmac_verification(settings: Settings) -> None:
    body = b'{"a":1}'
    from app.core.security import compute_hmac_sha256

    sig = compute_hmac_sha256(settings.hr_webhook_secret, body)
    assert verify_hmac_sha256(settings.hr_webhook_secret, body, sig)
    assert verify_hmac_sha256(settings.hr_webhook_secret, body, f"sha256={sig}")
    assert not verify_hmac_sha256(settings.hr_webhook_secret, body, "nope")
