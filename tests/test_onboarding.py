"""Onboarding config — read-open / write-restricted authz, plus step validation.

The checklist is employee-facing, so any authenticated employee may read it; only
HR/Admin may replace it (CLAUDE §9 — every protected endpoint gets an authz test).
Step links must stay internal and step ids must be unique.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_STEP = {
    "id": "agent",
    "title": "Install the agent",
    "description": "Set up tracking.",
    "action": "Install",
    "icon": "laptop",
    "tile": "mint",
    "required": True,
    "href": "/dashboard/download",
}
_CONFIG = {
    "enabled": True,
    "eyebrow": "Welcome aboard",
    "title": "Let's begin",
    "subtitle": "A quick setup.",
    "steps": [_STEP],
}


async def test_get_config_readable_by_any_employee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        "/api/v1/onboarding/config", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert len(body["steps"]) > 0  # default checklist is seeded on first read


async def test_update_forbidden_for_non_hr_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.put(
        "/api/v1/onboarding/config", json=_CONFIG, headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 403


async def test_admin_replaces_checklist_and_everyone_sees_it(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    put = await client.put(
        "/api/v1/onboarding/config", json=_CONFIG, headers=auth_headers(settings, seed.admin)
    )
    assert put.status_code == 200
    assert put.json()["title"] == "Let's begin"

    seen = await client.get(
        "/api/v1/onboarding/config", headers=auth_headers(settings, seed.report)
    )
    assert seen.status_code == 200
    body = seen.json()
    assert len(body["steps"]) == 1
    assert body["steps"][0]["id"] == "agent"


async def test_external_link_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    payload = {**_CONFIG, "steps": [{**_STEP, "href": "https://evil.example.com"}]}
    resp = await client.put(
        "/api/v1/onboarding/config", json=payload, headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 422


async def test_duplicate_step_ids_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    payload = {**_CONFIG, "steps": [_STEP, {**_STEP, "title": "Dupe"}]}
    resp = await client.put(
        "/api/v1/onboarding/config", json=payload, headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 422
