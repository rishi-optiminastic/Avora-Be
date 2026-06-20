"""Manual project-label correction — propose (self) + manager review (scoped)."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def _project(client: AsyncClient, settings: Settings, seed: _Seed) -> str:
    resp = await client.post(
        "/api/v1/work-entities",
        json={"name": "Circle", "keywords": ["circle"], "domains": []},
        headers=auth_headers(settings, seed.admin),
    )
    return str(resp.json()["id"])


async def test_propose_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.post("/api/v1/attribution/corrections", json={})).status_code == 401


async def test_employee_proposes_and_manager_reviews(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    project_id = await _project(client, settings, seed)
    # The report proposes a correction for their own work.
    proposed = await client.post(
        "/api/v1/attribution/corrections",
        json={"entity_id": project_id, "note": "this was Circle work"},
        headers=auth_headers(settings, seed.report),
    )
    assert proposed.status_code == 201, proposed.text
    cid = proposed.json()["id"]
    assert proposed.json()["status"] == "pending"

    # The manager sees it in scope and approves it.
    listed = await client.get(
        "/api/v1/attribution/corrections", headers=auth_headers(settings, seed.manager)
    )
    assert cid in [c["id"] for c in listed.json()]
    reviewed = await client.post(
        f"/api/v1/attribution/corrections/{cid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.manager),
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["reviewed_by_id"] == str(seed.manager.id)


async def test_non_manager_cannot_review(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    proposed = await client.post(
        "/api/v1/attribution/corrections",
        json={},
        headers=auth_headers(settings, seed.report),
    )
    cid = proposed.json()["id"]
    # The report (proposer, non-manager) cannot review their own correction.
    resp = await client.post(
        f"/api/v1/attribution/corrections/{cid}/review",
        json={"approve": True},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_outsider_cannot_see_or_review(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    proposed = await client.post(
        "/api/v1/attribution/corrections",
        json={},
        headers=auth_headers(settings, seed.report),
    )
    cid = proposed.json()["id"]
    # Out-of-scope reviewer gets 404 (never leaks existence).
    resp = await client.post(
        f"/api/v1/attribution/corrections/{cid}/review",
        json={"approve": False},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404
