"""Overview hero rollup — scoped to the caller, composed from live activity,
today's attendance, task counts, and approved leave."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_overview_is_scoped_and_well_formed(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        "/api/v1/dashboard/overview", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # A manager sees self + direct reports (manager + report) — never the outsider.
    ids = {p["employee_id"] for p in body["people"]}
    assert str(seed.manager.id) in ids
    assert str(seed.report.id) in ids
    assert str(seed.outsider.id) not in ids
    assert body["total"] == len(body["people"])

    # Counts are internally consistent.
    assert body["active"] + body["idle"] + body["offline"] == body["total"]
    # No activity seeded ⇒ everyone offline, no productivity, nobody present.
    assert body["offline"] == body["total"]
    assert body["avg_productivity_pct"] == 0
    assert body["present_today"] == 0

    # Every person row carries the live shape the hero renders.
    person = next(p for p in body["people"] if p["employee_id"] == str(seed.report.id))
    assert person["status"] == "offline"
    assert set(person) == {
        "employee_id",
        "name",
        "department",
        "job_title",
        "status",
        "productivity_pct",
        "active_window",
    }


async def test_overview_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/dashboard/overview")).status_code == 401
