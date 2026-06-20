"""Quick Meet — auth, the not-configured guard, and the service-account JWT.

The Google/Slack happy path needs real credentials, so it's validated manually;
here we cover the security-relevant logic: only authed users can start a meeting,
a missing config fails cleanly, and the SA assertion carries the right claims
(impersonation subject + Meet scope) so domain-wide delegation works.
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
    service = MeetingService(configured, EmployeeRepository(db), AuditRepository(db))

    assertion = service._build_assertion("alice@corp.test")
    claims = jwt.decode(
        assertion,
        public_pem,
        algorithms=["RS256"],
        audience=configured.google_sa_token_uri,
    )
    assert claims["iss"] == "avora-meet@proj.iam.gserviceaccount.com"
    assert claims["sub"] == "alice@corp.test"  # domain-wide delegation impersonation
    assert claims["scope"] == "https://www.googleapis.com/auth/meetings.space.created"
