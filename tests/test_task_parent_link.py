"""Task parent-linking: scope + self-parent + cycle validation."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def _create(
    client: AsyncClient, settings: Settings, actor: object, assignee_id: str, **extra: object
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/tasks",
        json={"title": "T", "assignee_id": assignee_id, "cadence": "one_time", **extra},
        headers=auth_headers(settings, actor),  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_with_valid_parent(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    parent = await _create(client, settings, seed.admin, str(seed.report.id))
    child = await _create(
        client, settings, seed.admin, str(seed.report.id), parent_task_id=parent["id"]
    )
    assert child["parent_task_id"] == parent["id"]


async def test_parent_must_be_visible(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    # A task the outsider can't see, used as a parent by the outsider → rejected.
    hidden = await _create(client, settings, seed.admin, str(seed.report.id))
    resp = await client.post(
        "/api/v1/tasks",
        json={
            "title": "mine",
            "assignee_id": str(seed.outsider.id),
            "cadence": "one_time",
            "parent_task_id": hidden["id"],
        },
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 422


async def test_self_parent_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    task = await _create(client, settings, seed.admin, str(seed.report.id))
    resp = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"parent_task_id": task["id"]},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422


async def test_cycle_rejected(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    a = await _create(client, settings, seed.admin, str(seed.report.id))
    b = await _create(client, settings, seed.admin, str(seed.report.id), parent_task_id=a["id"])
    # b's parent is a; making a's parent b would close a cycle → rejected.
    resp = await client.patch(
        f"/api/v1/tasks/{a['id']}",
        json={"parent_task_id": b["id"]},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422


async def test_assignee_may_link_own_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The report owns both tasks; a non-manager assignee may link them.
    parent = await _create(client, settings, seed.report, str(seed.report.id))
    child = await _create(client, settings, seed.report, str(seed.report.id))
    resp = await client.patch(
        f"/api/v1/tasks/{child['id']}",
        json={"parent_task_id": parent["id"]},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_task_id"] == parent["id"]
