"""Work-entity catalog authz + attribution derivation (matcher + scoping)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from app.core.config import Settings
from app.core.work_signals import attribute, build_profile
from tests.conftest import _Seed, auth_headers


# --- pure matcher ---------------------------------------------------------- #
def test_matcher_matches_distinctive_terms_and_unknown() -> None:
    avora = build_profile(uuid.uuid4(), "Avora", ["avora", "rishi-optiminastic"], ["github.com"])
    acme = build_profile(uuid.uuid4(), "Acme Deal", ["acme", "acmecorp"], ["acme.com"])
    profiles = [avora, acme]

    # OCR text containing the repo path + a github domain → strong Avora match.
    m = attribute("main.go - Avora - rishi-optiminastic/Avora", "github.com", profiles)
    assert m is not None and m.name == "Avora" and m.confidence > 0

    # Unrelated screen → Unknown.
    assert attribute("random personal blog about cooking", "example.com", profiles) is None


# --- catalog authz --------------------------------------------------------- #
async def test_work_entities_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {"name": "Avora", "keywords": ["avora"], "domains": ["github.com"]}
    # Non-admin (manager) cannot create or list.
    assert (
        await client.post(
            "/api/v1/work-entities", json=body, headers=auth_headers(settings, seed.manager)
        )
    ).status_code == 403
    assert (
        await client.get("/api/v1/work-entities", headers=auth_headers(settings, seed.manager))
    ).status_code == 403
    # Admin can.
    created = await client.post(
        "/api/v1/work-entities", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Avora"


async def test_attribution_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/attribution/now")).status_code == 401


async def test_attribution_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # Manager sees an attribution row per visible employee (self + report), scoped.
    resp = await client.get("/api/v1/attribution/now", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    ids = {r["employee_id"] for r in resp.json()}
    assert ids == {str(seed.manager.id), str(seed.report.id)}
    # No samples/entities yet ⇒ everyone Unknown.
    assert all(r["entity_id"] is None and r["confidence"] == 0 for r in resp.json())
