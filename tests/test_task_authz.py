"""Task authorization tests — every protected path proves scope is enforced.

Satisfies the §9 "every protected endpoint must have an authorization test"
requirement for the tasks module.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _new_task(assignee_id: str, title: str = "Ship it") -> dict[str, object]:
    return {"title": title, "assignee_id": assignee_id, "cadence": "daily"}


async def _create_as(
    client: AsyncClient, settings: Settings, actor, assignee_id: str
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/tasks", json=_new_task(assignee_id), headers=auth_headers(settings, actor)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/tasks")).status_code == 401


async def test_manager_can_assign_to_report(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    assert task["assignee_id"] == str(seed.report.id)
    assert task["assigned_by_id"] == str(seed.manager.id)


async def test_employee_cannot_create_tasks(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Individual contributors receive tasks; they don't author them — even
    # self-assignment is forbidden now (managers/HR/admin create).
    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.report.id)),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_employee_cannot_assign_to_peer(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # report assigning to outsider -> 403 (not a manager).
    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.outsider.id)),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_list_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await _create_as(client, settings, seed.manager, str(seed.report.id))  # report's task

    # The report sees their task.
    mine = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.report))
    assert mine.status_code == 200
    assert mine.json()["total"] == 1

    # The outsider sees none of it.
    theirs = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.outsider))
    assert theirs.status_code == 200
    assert theirs.json()["total"] == 0


async def test_outsider_cannot_read_task_gets_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    resp = await client.get(
        f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404


async def test_status_update_sets_completed_at(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    resp = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "done"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["completed_at"] is not None


async def test_assignee_cannot_edit_nonstatus_fields(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The assignee may move their task on the board, not retitle/reassign it.
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    resp = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed by employee"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


# --- projects (work_entities) on tasks ------------------------------------- #
async def _make_project(client: AsyncClient, settings: Settings, seed: _Seed) -> str:
    resp = await client.post(
        "/api/v1/work-entities",
        json={"name": "Vara Ads", "keywords": ["vara"], "domains": []},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_manager_can_attach_project(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    project_id = await _make_project(client, settings, seed)
    resp = await client.post(
        "/api/v1/tasks",
        json={**_new_task(str(seed.report.id)), "project_id": project_id},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_id"] == project_id


async def test_unknown_project_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/tasks",
        json={**_new_task(str(seed.report.id)), "project_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422


async def test_active_projects_is_manager_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _make_project(client, settings, seed)
    # Manager can read the picker list…
    ok = await client.get(
        "/api/v1/work-entities/active", headers=auth_headers(settings, seed.manager)
    )
    assert ok.status_code == 200
    assert any(p["name"] == "Vara Ads" for p in ok.json())
    # …an individual contributor cannot.
    no = await client.get(
        "/api/v1/work-entities/active", headers=auth_headers(settings, seed.report)
    )
    assert no.status_code == 403


async def test_admin_sees_all_and_can_delete(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    listing = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.admin))
    assert listing.json()["total"] == 1
    deleted = await client.delete(
        f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.admin)
    )
    assert deleted.status_code == 204


async def test_non_assigner_cannot_delete(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    # The assignee (report) is not the assigner and not admin -> 403.
    resp = await client.delete(
        f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 403
