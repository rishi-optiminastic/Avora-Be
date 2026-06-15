"""Holiday authorization tests — read = everyone, write = HR/admin only."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _body(name: str = "Diwali", day: str = "2026-11-08") -> dict[str, object]:
    return {"name": name, "date": day, "holiday_type": "public"}


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/holidays")).status_code == 401


async def test_admin_can_create_and_everyone_can_read(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/holidays", json=_body(), headers=auth_headers(settings, seed.admin)
    )
    assert created.status_code == 201

    # A plain employee can read the org calendar.
    listing = await client.get("/api/v1/holidays", headers=auth_headers(settings, seed.report))
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


async def test_non_hr_cannot_create(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        "/api/v1/holidays", json=_body(), headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 403


async def test_non_hr_cannot_delete(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    created = await client.post(
        "/api/v1/holidays", json=_body(), headers=auth_headers(settings, seed.admin)
    )
    hid = created.json()["id"]
    resp = await client.delete(
        f"/api/v1/holidays/{hid}", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 403


async def test_admin_can_delete(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    created = await client.post(
        "/api/v1/holidays", json=_body(), headers=auth_headers(settings, seed.admin)
    )
    hid = created.json()["id"]
    resp = await client.delete(
        f"/api/v1/holidays/{hid}", headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 204
