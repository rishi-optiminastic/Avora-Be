"""Security primitives: JWT verification, token/secret hashing, HMAC verify.

Pure functions only — no DB, no FastAPI objects. Every comparison of a secret
goes through `hmac.compare_digest` to avoid timing oracles.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import Settings

# Algorithms we will EVER accept. `none` and HS-downgrade attacks are rejected
# by construction because we pass an explicit allowlist to jwt.decode and never
# include "none" (Security rule 5.2).
_FORBIDDEN_ALGS = {"none", "None", "NONE"}


class TokenError(Exception):
    """Raised when a JWT is missing, malformed, expired, or untrusted."""


def create_session_jwt(
    settings: Settings,
    *,
    subject: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a short-lived session JWT for a human after OIDC verification.

    Note: the claims placed here are server-derived. We never copy a client's
    self-asserted role/team into a token — those are re-derived from the DB.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_access_ttl_seconds)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_session_jwt(settings: Settings, token: str) -> dict[str, Any]:
    """Verify signature, expiry, issuer, and audience. Reject `alg: none`."""
    if settings.jwt_algorithm in _FORBIDDEN_ALGS:
        raise TokenError("server misconfigured: 'none' algorithm")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],  # explicit allowlist — no downgrade
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc
    return claims


# Better Auth tokens are asymmetric (EdDSA). Symmetric (HS*) and `none` are
# excluded by construction: we pass a single configured asymmetric algorithm as
# the allowlist, so a token whose header claims HS256/none can never validate
# against an EdDSA public key (Security rule 5.2 — no HS-downgrade, no `none`).
_SYMMETRIC_PREFIX = "HS"


def decode_better_auth_jwt(settings: Settings, token: str, *, key: Any) -> dict[str, Any]:
    """Verify a Better Auth JWT against its JWKS public ``key``.

    Checks signature, expiry, issuer and audience, and requires the standard
    claims. ``key`` is the public key resolved from the JWKS for the token's
    ``kid`` — the network fetch lives in the dependency layer so this stays a
    pure function.
    """
    alg = settings.better_auth_jwt_algorithm
    if alg in _FORBIDDEN_ALGS or alg.upper().startswith(_SYMMETRIC_PREFIX):
        raise TokenError("server misconfigured: symmetric/none algorithm")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[alg],  # explicit asymmetric allowlist — no HS/none downgrade
            issuer=settings.better_auth_issuer,
            audience=settings.better_auth_audience,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc
    return claims


# --------------------------------------------------------------------------- #
# Per-device agent tokens — store only a peppered hash, never the raw token.
# --------------------------------------------------------------------------- #
def generate_device_token() -> str:
    """Generate a fresh, high-entropy device token (returned once, at enrollment)."""
    return secrets.token_urlsafe(32)


def hash_device_token(settings: Settings, raw_token: str) -> str:
    """One-way peppered hash of a device token for at-rest storage."""
    mac = hmac.new(
        settings.agent_token_pepper.encode(),
        raw_token.encode(),
        hashlib.sha256,
    )
    return mac.hexdigest()


# --------------------------------------------------------------------------- #
# Invitation tokens — the raw token travels in the emailed link; we persist
# only a peppered hash and look invites up by it.
# --------------------------------------------------------------------------- #
def generate_invite_token() -> str:
    """Fresh, high-entropy invite token (placed in the link, never stored)."""
    return secrets.token_urlsafe(32)


def hash_invite_token(settings: Settings, raw_token: str) -> str:
    """One-way peppered hash of an invite token for at-rest storage + lookup."""
    mac = hmac.new(
        settings.invite_token_pepper.encode(),
        raw_token.encode(),
        hashlib.sha256,
    )
    return mac.hexdigest()


def verify_device_token(settings: Settings, raw_token: str, stored_hash: str) -> bool:
    """Constant-time check of a presented device token against the stored hash."""
    candidate = hash_device_token(settings, raw_token)
    return hmac.compare_digest(candidate, stored_hash)


# --------------------------------------------------------------------------- #
# HMAC for signed payloads (agent ingest body, HR webhook body).
# --------------------------------------------------------------------------- #
def compute_hmac_sha256(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_hmac_sha256(secret: str, body: bytes, provided_signature: str) -> bool:
    """Constant-time HMAC verification. Tolerates a leading `sha256=` prefix."""
    if not provided_signature:
        return False
    provided = provided_signature.removeprefix("sha256=").strip()
    expected = compute_hmac_sha256(secret, body)
    return hmac.compare_digest(expected, provided)
