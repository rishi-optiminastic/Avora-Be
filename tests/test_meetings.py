"""Quick Meet — auth, the not-configured guard, and the service-account JWT.

The Google/Slack happy path needs real credentials, so it's validated manually;
here we cover the security-relevant logic: only authed users can start a meeting,
a missing config fails cleanly, the SA assertion carries the right claims
(impersonation subject + Calendar scope) so domain-wide delegation works, and a
caller's default invitees are strictly self-scoped.
"""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_settings
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.quick_meet import QuickMeetRepository
from app.services.meeting_service import MeetingService, _describe_pem
from tests.conftest import _Seed, auth_headers


async def test_quick_meet_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/meetings/quick")
    assert resp.status_code == 401


async def test_quick_meet_unconfigured_fails_cleanly(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Test settings carry no Google SA creds → feature is off → 503, not a 500.
    resp = await client.post("/api/v1/meetings/quick", headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "integration_unavailable"


def test_sa_assertion_has_impersonation_and_scope(settings: Settings, db: AsyncSession) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    configured = settings.model_copy(
        update={
            "google_sa_client_email": "avora-meet@proj.iam.gserviceaccount.com",
            "google_sa_private_key": private_pem,
        }
    )
    service = MeetingService(
        configured, EmployeeRepository(db), AuditRepository(db), QuickMeetRepository(db)
    )

    assertion = service._build_assertion("alice@corp.test")
    claims = jwt.decode(
        assertion,
        public_pem,
        algorithms=["RS256"],
        audience=configured.google_sa_token_uri,
    )
    assert claims["iss"] == "avora-meet@proj.iam.gserviceaccount.com"
    assert claims["sub"] == "alice@corp.test"  # domain-wide delegation impersonation
    assert claims["scope"] == "https://www.googleapis.com/auth/calendar.events"


async def test_quick_defaults_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/meetings/quick/defaults")).status_code == 401
    assert (
        await client.put("/api/v1/meetings/quick/defaults", json={"invitee_emails": []})
    ).status_code == 401


async def test_quick_defaults_are_self_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Each caller reads/writes only their own list; nobody can target another user.
    empty = await client.get(
        "/api/v1/meetings/quick/defaults", headers=auth_headers(settings, seed.admin)
    )
    assert empty.status_code == 200
    assert empty.json()["invitee_emails"] == []

    saved = await client.put(
        "/api/v1/meetings/quick/defaults",
        headers=auth_headers(settings, seed.admin),
        json={"invitee_emails": ["a@corp.com", "A@corp.com", "b@corp.com"]},
    )
    assert saved.status_code == 200
    # Case-insensitive de-dupe is applied server-side.
    assert saved.json()["invitee_emails"] == ["a@corp.com", "b@corp.com"]

    # A different caller still sees their own (empty) list, not the admin's.
    other = await client.get(
        "/api/v1/meetings/quick/defaults", headers=auth_headers(settings, seed.report)
    )
    assert other.status_code == 200
    assert other.json()["invitee_emails"] == []


async def test_malformed_sa_key_is_a_clean_503_not_a_500(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A key that is present but unreadable must fail as "misconfigured".

    `quick_meet_configured` only checks the env vars are non-empty, so a PEM with
    half-unescaped newlines sails past it and PyJWT then rejects it. That used to
    escape as an opaque 500 ("Could not start the meeting" with no reason); it now
    names the offending env var.
    """
    broken = settings.model_copy(
        update={
            "google_sa_client_email": "avora-meet@proj.iam.gserviceaccount.com",
            "google_sa_private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n",
        }
    )
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_settings] = lambda: broken
    try:
        resp = await client.post(
            "/api/v1/meetings/quick", headers=auth_headers(settings, seed.admin)
        )
    finally:
        app.dependency_overrides[get_settings] = lambda: settings

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "integration_unavailable"
    assert "GOOGLE_SA_PRIVATE_KEY" in resp.json()["error"]["message"]


def _pem() -> str:
    """A real, parseable RSA private key in canonical multi-line PEM form."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _signs(settings: Settings, db: AsyncSession, private_key: str) -> bool:
    """Can the service build a signed assertion from this pasted key?"""
    configured = settings.model_copy(
        update={
            "google_sa_client_email": "avora-meet@proj.iam.gserviceaccount.com",
            "google_sa_private_key": private_key,
        }
    )
    service = MeetingService(
        configured, EmployeeRepository(db), AuditRepository(db), QuickMeetRepository(db)
    )
    return bool(service._build_assertion("alice@corp.test"))


def test_key_pasted_with_escaped_newlines_is_accepted(settings: Settings, db: AsyncSession) -> None:
    """The form straight out of the service-account JSON."""
    assert _signs(settings, db, _pem().replace("\n", "\\n"))


def test_key_pasted_with_real_newlines_is_accepted(settings: Settings, db: AsyncSession) -> None:
    """What you get when the deploy UI expands the escapes for you."""
    assert _signs(settings, db, _pem())


def test_key_pasted_with_wrapping_quotes_is_accepted(settings: Settings, db: AsyncSession) -> None:
    """VALUE="-----BEGIN..." — the quotes come along for the ride."""
    assert _signs(settings, db, f'"{_pem()}"')
    assert _signs(settings, db, f"'{_pem().replace(chr(10), chr(92) + 'n')}'")


def test_key_with_surrounding_whitespace_is_accepted(settings: Settings, db: AsyncSession) -> None:
    assert _signs(settings, db, f"  \n{_pem()}\n  ")


def test_a_truncated_key_still_fails(settings: Settings, db: AsyncSession) -> None:
    """Normalising formatting must not paper over a genuinely broken key."""
    with pytest.raises((ValueError, jwt.exceptions.PyJWTError)):
        _signs(settings, db, "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n")


def test_describe_pem_names_the_actual_defect_without_leaking_the_key() -> None:
    secret = _pem()
    assert _describe_pem("") == "it is empty"
    assert "no BEGIN line" in _describe_pem("MIIEvgIBADANBg")
    assert "no line breaks" in _describe_pem(
        "-----BEGIN PRIVATE KEY----- MIIE -----END PRIVATE KEY-----"
    )
    # A well-formed key that simply didn't parse must not have its body echoed.
    described = _describe_pem(secret)
    assert "MII" not in described
