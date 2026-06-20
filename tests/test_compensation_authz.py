"""Compensation authorization — the tightest scope in the app.

Policy: HR/Admin read & write anyone's pay; the person reads only their own and
can never write; managers (even of a direct report) get nothing.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_BODY = {
    "amount_minor": 12_000_000,
    "bonus_minor": 1_000_000,
    "currency": "usd",
    "period": "annual",
}


async def test_admin_can_set_and_read(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    put = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_BODY,
        headers=auth_headers(settings, seed.admin),
    )
    assert put.status_code == 200
    body = put.json()
    assert body["amount_minor"] == 12_000_000
    assert body["currency"] == "USD"  # normalized upper-case
    assert body["period"] == "annual"

    got = await client.get(
        f"/api/v1/employees/{seed.report.id}/compensation",
        headers=auth_headers(settings, seed.admin),
    )
    assert got.status_code == 200
    assert got.json()["bonus_minor"] == 1_000_000


async def test_person_can_read_own_but_not_write(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_BODY,
        headers=auth_headers(settings, seed.admin),
    )

    own = await client.get(
        f"/api/v1/employees/{seed.report.id}/compensation",
        headers=auth_headers(settings, seed.report),
    )
    assert own.status_code == 200

    forbidden = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_BODY,
        headers=auth_headers(settings, seed.report),
    )
    assert forbidden.status_code == 403


async def test_manager_cannot_see_reports_compensation(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_BODY,
        headers=auth_headers(settings, seed.admin),
    )
    # The manager manages this report for everything else — but NOT pay.
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}/compensation",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_outsider_cannot_read(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_BODY,
        headers=auth_headers(settings, seed.admin),
    )
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}/compensation",
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 403


async def test_unset_compensation_is_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"/api/v1/employees/{seed.outsider.id}/compensation",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404
