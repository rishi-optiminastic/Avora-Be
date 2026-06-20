"""Regularization — self request, manager review, monthly credit cap, scoping."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def _request(client: AsyncClient, settings: Settings, actor, day: str):
    return await client.post(
        "/api/v1/attendance/regularizations",
        json={"day": day, "reason": "stuck in traffic"},
        headers=auth_headers(settings, actor),
    )


async def test_request_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/attendance/regularizations", json={"day": "2026-06-01"})
    assert resp.status_code == 401


async def test_request_and_manager_approves(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    req = await _request(client, settings, seed.report, "2026-06-02")
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert req.json()["status"] == "pending"

    # Manager sees it in scope and approves.
    listed = await client.get(
        "/api/v1/attendance/regularizations", headers=auth_headers(settings, seed.manager)
    )
    assert rid in [r["id"] for r in listed.json()]
    ok = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


async def test_duplicate_day_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    assert (await _request(client, settings, seed.report, "2026-06-03")).status_code == 201
    assert (await _request(client, settings, seed.report, "2026-06-03")).status_code == 422


async def test_non_manager_cannot_review(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    rid = (await _request(client, settings, seed.report, "2026-06-04")).json()["id"]
    resp = await client.post(
        f"/api/v1/attendance/regularizations/{rid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_monthly_credit_cap(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # Default policy allows 2 approvals/month; the 3rd is rejected.
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        await _request(client, settings, seed.report, d)
    listed = (
        await client.get(
            "/api/v1/attendance/regularizations?month=2026-07",
            headers=auth_headers(settings, seed.manager),
        )
    ).json()
    ids = [r["id"] for r in listed]
    results = []
    for rid in ids:
        r = await client.post(
            f"/api/v1/attendance/regularizations/{rid}/review",
            json={"approve": True},
            headers=auth_headers(settings, seed.manager),
        )
        results.append(r.status_code)
    assert results.count(200) == 2
    assert 422 in results  # the 3rd exceeds the monthly credit cap
