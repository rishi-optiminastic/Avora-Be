"""Device authorization tests — enroll/revoke = admin/IT only; reads are scoped."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/devices")).status_code == 401


async def test_admin_can_enroll_returns_token_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/devices",
        json={"employee_id": str(seed.outsider.id), "label": "olive-mbp"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["employee_id"] == str(seed.outsider.id)
    assert body["token"]  # raw token shown once


async def test_non_admin_cannot_enroll(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/devices",
        json={"employee_id": str(seed.report.id), "label": "x"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_list_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # The seed enrolls one device for the report.
    mgr = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.manager))
    assert mgr.status_code == 200
    assert any(d["employee_id"] == str(seed.report.id) for d in mgr.json())

    outsider = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.outsider))
    assert outsider.json() == []


async def test_admin_can_revoke_non_admin_cannot(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    denied = await client.post(
        f"/api/v1/devices/{seed.device.id}/revoke", headers=auth_headers(settings, seed.manager)
    )
    assert denied.status_code == 403

    ok = await client.post(
        f"/api/v1/devices/{seed.device.id}/revoke", headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 200
    assert ok.json()["is_revoked"] is True
