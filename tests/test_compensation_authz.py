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


# ---- bank details: the person may set their own (unlike pay) --------------- #

_BANK = {
    "account_holder_name": "Rishi Patel",
    "bank_name": "HDFC Bank",
    "account_number": "50100123456789",
    "ifsc_code": "hdfc0001234",  # lower-case in -> upper-case out
    "account_type": "savings",
}


async def test_person_can_set_own_bank_and_number_round_trips(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    put = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation/bank",
        json=_BANK,
        headers=auth_headers(settings, seed.report),  # the person themselves
    )
    assert put.status_code == 200
    body = put.json()
    assert body["account_holder_name"] == "Rishi Patel"
    assert body["ifsc_code"] == "HDFC0001234"  # normalized upper-case
    assert body["account_number"] == "50100123456789"  # decrypted round-trip
    assert body["account_type"] == "savings"


async def test_outsider_cannot_set_someone_elses_bank(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation/bank",
        json=_BANK,
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 403


async def test_bad_ifsc_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation/bank",
        json={**_BANK, "ifsc_code": "nope"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422
