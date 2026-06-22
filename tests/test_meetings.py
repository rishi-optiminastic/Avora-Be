"""Quick Meet — auth, the not-configured guard, and the service-account JWT.

The Google/Slack happy path needs real credentials, so it's validated manually;
here we cover the security-relevant logic: only authed users can start a meeting,
a missing config fails cleanly, the SA assertion carries the right claims
(impersonation subject + Calendar scope) so domain-wide delegation works, and a
caller's default invitees are strictly self-scoped.
"""

from __future__ import annotations

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.quick_meet import QuickMeetRepository
from app.services.meeting_service import MeetingService
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
