"""Dashboard rollups — scoped delayed tasks, department perf, project manpower."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_rollups_require_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/dashboard/delayed-tasks")).status_code == 401
    assert (await client.get("/api/v1/dashboard/department-performance")).status_code == 401


async def test_delayed_tasks_lists_overdue(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # An overdue, not-done task shows up; a future-due one does not.
    overdue = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Late one",
            "assignee_id": str(seed.report.id),
            "due_date": "2020-01-01T00:00:00Z",
        },
        headers=auth_headers(settings, seed.manager),
    )
    assert overdue.status_code == 201, overdue.text
    await client.post(
        "/api/v1/tasks",
        json={
            "title": "Future one",
            "assignee_id": str(seed.report.id),
            "due_date": "2999-01-01T00:00:00Z",
        },
        headers=auth_headers(settings, seed.manager),
    )
    resp = await client.get(
        "/api/v1/dashboard/delayed-tasks", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()]
    assert "Late one" in titles
    assert "Future one" not in titles

    # The out-of-scope outsider sees none of the manager's team's delays.
    outsider = await client.get(
        "/api/v1/dashboard/delayed-tasks", headers=auth_headers(settings, seed.outsider)
    )
    assert all(t["title"] != "Late one" for t in outsider.json())


async def test_project_manpower_counts_by_project(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    project = await client.post(
        "/api/v1/work-entities",
        json={"name": "Avora", "keywords": ["avora"], "domains": []},
        headers=auth_headers(settings, seed.admin),
    )
    pid = project.json()["id"]
    await client.post(
        "/api/v1/tasks",
        json={"title": "T1", "assignee_id": str(seed.report.id), "project_id": pid},
        headers=auth_headers(settings, seed.manager),
    )
    resp = await client.get(
        "/api/v1/dashboard/project-manpower", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["project_id"] == pid)
    assert row["project_name"] == "Avora"
    assert row["people"] == 1
    assert row["open_tasks"] == 1
