"""Resignation authorization — employee submits/withdraws; HR/Admin decide."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _body() -> dict[str, object]:
    last_day = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    return {"reason": "Moving on", "last_working_day": last_day}


async def _submit(client: AsyncClient, settings: Settings, actor: object) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/resignations", json=_body(), headers=auth_headers(settings, actor)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unauthenticated_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/resignations")).status_code == 401


async def test_last_working_day_cannot_be_past(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    resp = await client.post(
        "/api/v1/resignations",
        json={"last_working_day": past},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 422


async def test_employee_submits_and_sees_only_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    mine = await _submit(client, settings, seed.report)
    await _submit(client, settings, seed.outsider)  # a different person's resignation

    listed = await client.get("/api/v1/resignations", headers=auth_headers(settings, seed.report))
    ids = [r["id"] for r in listed.json()["items"]]
    assert ids == [mine["id"]]  # the report sees only their own


async def test_duplicate_active_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _submit(client, settings, seed.report)
    dup = await client.post(
        "/api/v1/resignations", json=_body(), headers=auth_headers(settings, seed.report)
    )
    assert dup.status_code == 409


async def test_only_hr_admin_decides_never_own(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _submit(client, settings, seed.report)
    rid = res["id"]

    # The employee can't decide their own resignation.
    self_decide = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True},
        headers=auth_headers(settings, seed.report),
    )
    assert self_decide.status_code == 403

    # A manager (not HR/Admin) can't decide either.
    mgr = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert mgr.status_code == 403

    # Admin accepts it.
    ok = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": True, "note": "All the best"},
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "accepted"

    # A decided resignation can't be decided again.
    again = await client.post(
        f"/api/v1/resignations/{rid}/decision",
        json={"accept": False},
        headers=auth_headers(settings, seed.admin),
    )
    assert again.status_code == 409


async def test_employee_withdraws_own(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    res = await _submit(client, settings, seed.report)
    rid = res["id"]
    # An outsider can't withdraw someone else's resignation.
    other = await client.post(
        f"/api/v1/resignations/{rid}/withdraw", headers=auth_headers(settings, seed.outsider)
    )
    assert other.status_code == 403
    mine = await client.post(
        f"/api/v1/resignations/{rid}/withdraw", headers=auth_headers(settings, seed.report)
    )
    assert mine.status_code == 200
    assert mine.json()["status"] == "withdrawn"
