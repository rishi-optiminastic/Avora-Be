"""Changelog authorization tests.

Every employee may read the changelog; only an admin may publish, edit, or
delete. Satisfies the §9 "every protected endpoint has an authorization test"
requirement.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_only_admin_can_publish_but_everyone_reads(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    entry = {"title": "Dark mode", "body": "You can now switch themes.", "category": "feature"}

    # A manager (non-admin) cannot publish.
    forbidden = await client.post(
        "/api/v1/changelog", json=entry, headers=auth_headers(settings, seed.manager)
    )
    assert forbidden.status_code == 403

    # An admin can publish.
    created = await client.post(
        "/api/v1/changelog", json=entry, headers=auth_headers(settings, seed.admin)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "Dark mode"
    assert body["category"] == "feature"
    entry_id = body["id"]

    # Every authenticated employee can read it.
    listed = await client.get("/api/v1/changelog", headers=auth_headers(settings, seed.report))
    assert listed.status_code == 200
    assert any(item["id"] == entry_id for item in listed.json()["items"])

    # A non-admin cannot delete.
    blocked = await client.delete(
        f"/api/v1/changelog/{entry_id}", headers=auth_headers(settings, seed.report)
    )
    assert blocked.status_code == 403

    # An admin can delete.
    removed = await client.delete(
        f"/api/v1/changelog/{entry_id}", headers=auth_headers(settings, seed.admin)
    )
    assert removed.status_code == 204


async def test_publish_rejects_unknown_category(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/changelog",
        json={"title": "X", "body": "Y", "category": "not-a-category"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422
