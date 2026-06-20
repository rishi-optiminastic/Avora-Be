"""Target authorization — only admins/senior managers assign; owners track."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _new_target(owner_id: str) -> dict[str, object]:
    return {
        "title": "Q3 revenue",
        "owner_id": owner_id,
        "period": "monthly",
        "metric": "Revenue",
        "target_value": 100,
    }


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/targets")).status_code == 401


async def test_manager_cannot_assign_targets(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # A plain manager may NOT assign targets (only admin / senior manager).
    resp = await client.post(
        "/api/v1/targets",
        json=_new_target(str(seed.manager.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_admin_assigns_and_owner_tracks(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/targets",
        json=_new_target(str(seed.manager.id)),
        headers=auth_headers(settings, seed.admin),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["assigned_by_id"] == str(seed.admin.id)
    assert body["progress_pct"] == 0
    tid = body["id"]

    # The owner (manager) reports progress on their own target.
    progressed = await client.patch(
        f"/api/v1/targets/{tid}",
        json={"current_value": 50, "status": "on_track"},
        headers=auth_headers(settings, seed.manager),
    )
    assert progressed.status_code == 200
    assert progressed.json()["current_value"] == 50
    assert progressed.json()["progress_pct"] == 50

    # …but cannot re-scope it (e.g. change the target value).
    blocked = await client.patch(
        f"/api/v1/targets/{tid}",
        json={"target_value": 10},
        headers=auth_headers(settings, seed.manager),
    )
    assert blocked.status_code == 403


async def test_target_read_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    created = await client.post(
        "/api/v1/targets",
        json=_new_target(str(seed.manager.id)),
        headers=auth_headers(settings, seed.admin),
    )
    tid = created.json()["id"]
    # The unrelated outsider can't read it (404, never leaks existence).
    resp = await client.get(f"/api/v1/targets/{tid}", headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 404
