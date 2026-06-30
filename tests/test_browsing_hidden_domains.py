"""Personal hidden-domains feature: single-owner gating + browsing filter."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import generate_device_token, hash_device_token
from app.models import Device, Employee, EmployeeStatus, Role
from tests.conftest import _Seed, agent_headers, auth_headers


async def _make_owner(db: AsyncSession, settings: Settings) -> tuple[Employee, str]:
    """An employee whose work email is the configured hidden-domains owner,
    with a device enrolled so we can ingest browsing samples for them."""
    owner = Employee(
        hr_external_id="hr-owner",
        work_email=settings.private_browsing_owner_email,
        full_name="Owner Operator",
        role=Role.EMPLOYEE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(owner)
    await db.flush()
    raw_token = generate_device_token()
    db.add(
        Device(
            employee_id=owner.id,
            label="owner-laptop",
            token_hash=hash_device_token(settings, raw_token),
            last_sequence=0,
        )
    )
    await db.commit()
    return owner, raw_token


async def _ingest_url(client: AsyncClient, raw_token: str, sequence: int, url: str) -> None:
    sample = {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Google Chrome",
        "idle_seconds": 2,
        "url": url,
    }
    raw, headers = agent_headers(raw_token, sample)
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_hidden_domains_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/browsing/hidden-domains")).status_code == 401


async def test_non_owner_cannot_see_or_manage(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Even an admin who is NOT the owner gets 404 — the feature is invisible.
    admin = auth_headers(settings, seed.admin)
    assert (await client.get("/api/v1/browsing/hidden-domains", headers=admin)).status_code == 404
    assert (
        await client.post(
            "/api/v1/browsing/hidden-domains", json={"domain": "facebook.com"}, headers=admin
        )
    ).status_code == 404


async def test_owner_crud_and_validation(
    client: AsyncClient, settings: Settings, db: AsyncSession, seed: _Seed
) -> None:
    owner, _ = await _make_owner(db, settings)
    headers = auth_headers(settings, owner)

    # Empty to start.
    assert (await client.get("/api/v1/browsing/hidden-domains", headers=headers)).json() == []

    # A full URL is normalised to its host; www is stripped.
    created = await client.post(
        "/api/v1/browsing/hidden-domains",
        json={"domain": "https://www.Facebook.com/adsmanager"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["domain"] == "facebook.com"

    # Idempotent: hiding the same host again returns the same row, not a dupe.
    again = await client.post(
        "/api/v1/browsing/hidden-domains", json={"domain": "facebook.com"}, headers=headers
    )
    assert again.json()["id"] == created.json()["id"]
    listing = (await client.get("/api/v1/browsing/hidden-domains", headers=headers)).json()
    assert [d["domain"] for d in listing] == ["facebook.com"]

    # Garbage that yields no host is rejected.
    bad = await client.post(
        "/api/v1/browsing/hidden-domains", json={"domain": "   "}, headers=headers
    )
    assert bad.status_code in (404, 422)  # validation runs after the owner gate passes

    # Unhide.
    assert (
        await client.delete(
            f"/api/v1/browsing/hidden-domains/{created.json()['id']}", headers=headers
        )
    ).status_code == 204
    assert (await client.get("/api/v1/browsing/hidden-domains", headers=headers)).json() == []


async def test_hidden_domain_removed_from_browsing_for_everyone(
    client: AsyncClient, settings: Settings, db: AsyncSession, seed: _Seed
) -> None:
    owner, raw_token = await _make_owner(db, settings)
    await _ingest_url(client, raw_token, 1, "https://github.com/optiminastic/avora")
    await _ingest_url(client, raw_token, 2, "https://www.youtube.com/watch?v=x")
    await _ingest_url(client, raw_token, 3, "https://adsmanager.facebook.com/manage")

    owner_h = auth_headers(settings, owner)

    def owner_row(payload: list[dict]) -> dict:
        return next(r for r in payload if r["employee_id"] == str(owner.id))

    before = owner_row((await client.get("/api/v1/browsing", headers=owner_h)).json())
    assert before["total_minutes"] == 3
    assert {d["domain"] for d in before["top_domains"]} == {
        "github.com",
        "youtube.com",
        "adsmanager.facebook.com",
    }

    # Hide facebook.com — a subdomain (adsmanager.facebook.com) must vanish too.
    await client.post(
        "/api/v1/browsing/hidden-domains", json={"domain": "facebook.com"}, headers=owner_h
    )

    after = owner_row((await client.get("/api/v1/browsing", headers=owner_h)).json())
    assert {d["domain"] for d in after["top_domains"]} == {"github.com", "youtube.com"}
    # Time + focus recompute as if the hidden domain was never browsed (no tell).
    assert after["total_minutes"] == 2
    assert after["focus_pct"] == 50

    # And it's hidden from OTHER viewers too — the admin sees the owner's row
    # without the suppressed domain.
    admin_view = owner_row(
        (await client.get("/api/v1/browsing", headers=auth_headers(settings, seed.admin))).json()
    )
    assert all("facebook.com" not in d["domain"] for d in admin_view["top_domains"])
