"""Task authorization tests — every protected path proves scope is enforced.

Satisfies the §9 "every protected endpoint must have an authorization test"
requirement for the tasks module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


def _new_task(assignee_id: str, title: str = "Ship it") -> dict[str, object]:
    return {"title": title, "assignee_id": assignee_id, "cadence": "daily"}


async def test_due_date_cannot_be_in_the_past(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    resp = await client.post(
        "/api/v1/tasks",
        json={**_new_task(str(seed.report.id)), "due_date": past},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422, "a task can't be created already overdue"

    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    ok = await client.post(
        "/api/v1/tasks",
        json={**_new_task(str(seed.report.id)), "due_date": future},
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 201


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


async def test_employee_can_create_own_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Individual contributors may plan their own work — self-assignment is allowed.
    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.report.id)),
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 201
    assert resp.json()["assignee_id"] == str(seed.report.id)


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
    # A task linked only via project_id still surfaces the project's name so the
    # board/card can show it without a separate lookup.
    assert resp.json()["project"] == "Vara Ads"


async def test_project_name_follows_relink(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Re-pointing a task's project_id updates the resolved project name in the
    response (the selectin relation is refreshed, not left stale)."""
    first = await _make_project(client, settings, seed)
    created = await client.post(
        "/api/v1/tasks",
        json={**_new_task(str(seed.report.id)), "project_id": first},
        headers=auth_headers(settings, seed.manager),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    second = await client.post(
        "/api/v1/work-entities",
        json={"name": "Circle", "keywords": ["circle"], "domains": []},
        headers=auth_headers(settings, seed.admin),
    )
    assert second.status_code == 201, second.text
    relinked = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"project_id": second.json()["id"]},
        headers=auth_headers(settings, seed.manager),
    )
    assert relinked.status_code == 200, relinked.text
    assert relinked.json()["project"] == "Circle"

    # Clearing the link falls back to no project name.
    cleared = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"project_id": None},
        headers=auth_headers(settings, seed.manager),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["project"] is None


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


async def test_assignee_can_report_progress(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The assignee may update progress fields (%, blocker) on their own task…
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    ok = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"completion_pct": 60, "blocked_reason": "waiting on design"},
        headers=auth_headers(settings, seed.report),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["completion_pct"] == 60
    # …but not the manager-only review notes.
    blocked = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"review_notes": "great"},
        headers=auth_headers(settings, seed.report),
    )
    assert blocked.status_code == 403


async def test_escalate_is_manager_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    assert (
        await client.post(
            f"/api/v1/tasks/{task['id']}/escalate", headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    ok = await client.post(
        f"/api/v1/tasks/{task['id']}/escalate", headers=auth_headers(settings, seed.manager)
    )
    assert ok.status_code == 200
    assert ok.json()["escalated"] is True
    # Escalation is repeatable — a second escalation still succeeds (no
    # "already escalated" guard) so a manager can re-flag attention.
    again = await client.post(
        f"/api/v1/tasks/{task['id']}/escalate", headers=auth_headers(settings, seed.manager)
    )
    assert again.status_code == 200
    assert again.json()["escalated"] is True


async def test_appreciate_requires_done_and_is_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    # A task that isn't done yet can't be appreciated.
    early = await client.post(
        f"/api/v1/tasks/{task['id']}/appreciate",
        json={"note": "great"},
        headers=auth_headers(settings, seed.manager),
    )
    assert early.status_code == 422
    done = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "done"},
        headers=auth_headers(settings, seed.manager),
    )
    assert done.status_code == 200
    # The assignee is neither a manager nor the assigner → forbidden.
    blocked = await client.post(
        f"/api/v1/tasks/{task['id']}/appreciate",
        json={},
        headers=auth_headers(settings, seed.report),
    )
    assert blocked.status_code == 403
    # The manager (assigner) may appreciate a completed task.
    ok = await client.post(
        f"/api/v1/tasks/{task['id']}/appreciate",
        json={"note": "well done"},
        headers=auth_headers(settings, seed.manager),
    )
    assert ok.status_code == 200


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


# -- Bulk create (paste-import) --------------------------------------------- #


async def test_manager_can_bulk_create(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {
        "tasks": [
            _new_task(str(seed.report.id), "Bulk one"),
            _new_task(str(seed.manager.id), "Bulk two"),
        ]
    }
    resp = await client.post(
        "/api/v1/tasks/bulk", json=body, headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert {t["title"] for t in created} == {"Bulk one", "Bulk two"}


async def test_employee_cannot_bulk_create(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {"tasks": [_new_task(str(seed.report.id))]}
    resp = await client.post(
        "/api/v1/tasks/bulk", json=body, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 403


async def test_bulk_create_is_atomic_on_out_of_scope_assignee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # One in-scope (report) + one out-of-scope (outsider) -> whole batch rejected,
    # and nothing is created.
    body = {
        "tasks": [
            _new_task(str(seed.report.id), "Should not persist"),
            _new_task(str(seed.outsider.id), "Out of scope"),
        ]
    }
    resp = await client.post(
        "/api/v1/tasks/bulk", json=body, headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 403
    listing = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.manager))
    assert listing.json()["total"] == 0


# -- Free-text parse -------------------------------------------------------- #


async def test_parse_requires_manager(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        "/api/v1/tasks/parse",
        json={"text": "Remy -\n  do a thing"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


# (Parsing past the manager gate calls OpenRouter — covered by the manager-gate
# test above; we don't exercise the live model in the unit suite.)


# -- Collaborators ---------------------------------------------------------- #


async def test_collaborator_gains_visibility_and_progress_rights(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    # Outsider can't see the task yet.
    assert (
        await client.get(
            f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.outsider)
        )
    ).status_code == 404

    # Admin (sees everyone) adds the outsider as a collaborator.
    added = await client.post(
        f"/api/v1/tasks/{task['id']}/collaborators",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.admin),
    )
    assert added.status_code == 200, added.text
    assert str(seed.outsider.id) in added.json()["collaborator_ids"]

    # Now the outsider can read it...
    assert (
        await client.get(
            f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.outsider)
        )
    ).status_code == 200
    # ...report progress (status)...
    status_patch = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "in_progress"},
        headers=auth_headers(settings, seed.outsider),
    )
    assert status_patch.status_code == 200
    # ...but NOT retitle it.
    title_patch = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Hijacked"},
        headers=auth_headers(settings, seed.outsider),
    )
    assert title_patch.status_code == 403


async def test_remove_collaborator_revokes_visibility(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    await client.post(
        f"/api/v1/tasks/{task['id']}/collaborators",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.admin),
    )
    removed = await client.delete(
        f"/api/v1/tasks/{task['id']}/collaborators/{seed.outsider.id}",
        headers=auth_headers(settings, seed.admin),
    )
    assert removed.status_code == 200
    assert str(seed.outsider.id) not in removed.json()["collaborator_ids"]
    # Access is gone again.
    assert (
        await client.get(
            f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.outsider)
        )
    ).status_code == 404


async def test_cannot_add_collaborator_to_invisible_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    # Outsider can't see the task, so they can't manage its collaborators (404 so
    # we never reveal the task exists).
    resp = await client.post(
        f"/api/v1/tasks/{task['id']}/collaborators",
        json={"employee_id": str(seed.report.id)},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code == 404


async def test_manager_cannot_add_out_of_scope_collaborator(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    task = await _create_as(client, settings, seed.manager, str(seed.report.id))
    # The manager can see the task but not the outsider -> can't add them.
    resp = await client.post(
        f"/api/v1/tasks/{task['id']}/collaborators",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403
