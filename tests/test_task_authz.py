"""Task authorization tests — every protected path proves scope is enforced.

Satisfies the §9 "every protected endpoint must have an authorization test"
requirement for the tasks module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.task import Task, TaskCadence, TaskStatus
from app.repositories.assignment_grant import AssignmentGrantRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.task import TaskRepository
from app.repositories.task_comment import TaskCommentRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.services.llm_service import LlmError
from app.services.notification_service import NotificationService
from app.services.task_service import MY_OVERDUE_LIMIT, TaskService
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


async def test_my_overdue_returns_only_the_callers_own_open_overdue_tasks(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The reminder feed is the caller's OWN overdue work - never the whole
    scope, not even for an admin, whose delayed-tasks view is the entire org."""
    now = datetime.now(UTC)
    late, later = now - timedelta(days=3), now - timedelta(days=1)

    def _task(assignee: object, title: str, due: datetime | None, **kw: object) -> Task:
        return Task(
            title=title, assignee_id=assignee, cadence=TaskCadence.ONE_TIME, due_date=due, **kw
        )

    db.add_all(
        [
            _task(seed.admin.id, "admin newer", later),
            _task(seed.admin.id, "admin older", late, status=TaskStatus.IN_PROGRESS),
            _task(seed.admin.id, "admin done", late, status=TaskStatus.DONE),
            _task(seed.admin.id, "admin future", now + timedelta(days=2)),
            _task(seed.admin.id, "admin undated", None),
            _task(seed.report.id, "report late", late),
        ]
    )
    await db.commit()

    admin = await client.get(
        "/api/v1/tasks/mine/overdue", headers=auth_headers(settings, seed.admin)
    )
    assert admin.status_code == 200
    assert [t["title"] for t in admin.json()] == ["admin older", "admin newer"]

    report = await client.get(
        "/api/v1/tasks/mine/overdue", headers=auth_headers(settings, seed.report)
    )
    assert [t["title"] for t in report.json()] == ["report late"]

    outsider = await client.get(
        "/api/v1/tasks/mine/overdue", headers=auth_headers(settings, seed.outsider)
    )
    assert outsider.status_code == 200 and outsider.json() == []

    assert (await client.get("/api/v1/tasks/mine/overdue")).status_code == 401


async def test_my_overdue_is_capped(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    due = datetime.now(UTC) - timedelta(days=2)
    db.add_all(
        Task(title=f"t{i}", assignee_id=seed.report.id, cadence=TaskCadence.ONE_TIME, due_date=due)
        for i in range(MY_OVERDUE_LIMIT + 3)
    )
    await db.commit()
    res = await client.get(
        "/api/v1/tasks/mine/overdue", headers=auth_headers(settings, seed.report)
    )
    assert res.status_code == 200
    assert len(res.json()) == MY_OVERDUE_LIMIT


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


async def test_parse_works_with_no_llm_configured(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The structured format needs no model at all.

    Paste-import used to fail outright whenever OPENROUTER_API_KEY / the model
    were unset - it raised "Task parsing isn't configured." before reading a
    single line. The test settings have no key, which is exactly that case.
    """
    assert not settings.task_parse_configured  # the regression's precondition

    resp = await client.post(
        "/api/v1/tasks/parse",
        json={"text": "Remy -\n- Ship the thing\n- Fix the other thing\n"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert [t["title"] for t in tasks] == ["Ship the thing", "Fix the other thing"]
    # "Remy -" resolved to Remy Report, who is in the manager's scope.
    assert {t["assignee_id"] for t in tasks} == {str(seed.report.id)}


async def test_parse_resolves_a_name_only_inside_the_callers_scope(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The roster handed to the matcher is the caller's visible people, so a
    header naming someone out of scope resolves to nobody - it must not become a
    task assigned across a team boundary (§5.3)."""
    resp = await client.post(
        "/api/v1/tasks/parse",
        json={"text": "Olive -\n- Out of scope work\n"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    (task,) = resp.json()["tasks"]
    assert task["assignee_id"] is None
    # The label still comes back, so the UI can show what needs fixing.
    assert task["assignee_name"] == "Olive"


async def test_parse_reports_a_clear_error_when_it_can_read_nothing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/tasks/parse",
        json={"text": "-----\n\n=====\n"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 422
    assert "own line" in resp.text  # tells the user the format, not just "failed"


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


# Assignment grants ---------------------------------------------------------- #
# The reporting tree gives a manager their direct reports. A grant is the extra,
# explicit edge for authority the tree can't express (two managers, one report).
# It is assign-only and must never widen the employee read scope.


async def _grant(
    client: AsyncClient, settings: Settings, seed: _Seed, assignee_ids: list[str]
) -> None:
    """Admin grants the seed manager the right to assign to `assignee_ids`."""
    resp = await client.put(
        f"/api/v1/employees/{seed.manager.id}/assignment-grants",
        json={"assignee_ids": assignee_ids},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text


async def test_manager_cannot_assign_to_ungranted_outsider(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The baseline the grant is measured against: no grant, no assignment."""
    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.outsider.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_grant_lets_manager_assign_outside_their_reports(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _grant(client, settings, seed, [str(seed.outsider.id)])

    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.outsider.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["assignee_id"] == str(seed.outsider.id)
    assert resp.json()["assigned_by_id"] == str(seed.manager.id)


async def test_grant_does_not_widen_the_read_scope(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A grant is assign-only — it must not leak the granted person's profile."""
    await _grant(client, settings, seed, [str(seed.outsider.id)])

    resp = await client.get(
        f"/api/v1/employees/{seed.outsider.id}", headers=auth_headers(settings, seed.manager)
    )
    assert resp.status_code == 404, "granting assignment must not expose the profile"


async def test_revoking_a_grant_removes_the_ability(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _grant(client, settings, seed, [str(seed.outsider.id)])
    # PUT is a full replace — an empty list revokes everything.
    await _grant(client, settings, seed, [])

    resp = await client.post(
        "/api/v1/tasks",
        json=_new_task(str(seed.outsider.id)),
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_only_admin_or_hr_may_manage_grants(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.manager, seed.report, seed.outsider):
        write = await client.put(
            f"/api/v1/employees/{seed.manager.id}/assignment-grants",
            json={"assignee_ids": [str(seed.outsider.id)]},
            headers=auth_headers(settings, actor),
        )
        assert write.status_code == 403, "granting yourself authority must be admin/HR only"
        read = await client.get(
            f"/api/v1/employees/{seed.manager.id}/assignment-grants",
            headers=auth_headers(settings, actor),
        )
        assert read.status_code == 403


async def test_assignable_list_reflects_scope_plus_grants(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The picker's source of truth must match what create actually allows."""

    async def assignable(actor: object) -> set[str]:
        resp = await client.get(
            "/api/v1/employees/assignable", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 200, resp.text
        return {row["id"] for row in resp.json()}

    # An individual contributor has only their own work to plan.
    assert await assignable(seed.report) == {str(seed.report.id)}

    # A manager: themselves + their reports, and NOT the outsider yet.
    before = await assignable(seed.manager)
    assert {str(seed.manager.id), str(seed.report.id)} <= before
    assert str(seed.outsider.id) not in before

    await _grant(client, settings, seed, [str(seed.outsider.id)])
    assert str(seed.outsider.id) in await assignable(seed.manager)


async def test_grant_applies_to_bulk_create_too(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _grant(client, settings, seed, [str(seed.outsider.id)])

    resp = await client.post(
        "/api/v1/tasks/bulk",
        json={"tasks": [_new_task(str(seed.outsider.id), "Granted bulk")]},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 201, resp.text


# -- Paste-import fallback to the model ------------------------------------- #


class _StubParseLlm:
    """Stands in for OpenRouter. Returns (title, assignee_name) pairs - the model
    is never given ids, so this is the whole surface it can influence."""

    def __init__(self, items: list[tuple[str, str | None]] | None = None) -> None:
        self.items = items or []
        self.calls = 0
        self.texts: list[str] = []

    @property
    def task_parse_ready(self) -> bool:
        return True

    async def parse_tasks(self, text: str) -> list[tuple[str, str | None]]:
        self.calls += 1
        self.texts.append(text)
        return self.items


def _task_service(db: AsyncSession, llm: object) -> TaskService:
    return TaskService(
        TaskRepository(db),
        EmployeeRepository(db),
        AssignmentGrantRepository(db),
        WorkEntityRepository(db),
        TaskCommentRepository(db),
        AuditRepository(db),
        NotificationService(NotificationRepository(db)),
        _NoEmail(),  # type: ignore[arg-type]
        llm,  # type: ignore[arg-type]
    )


class _NoEmail:
    async def send_task_assigned(self, **kw: object) -> None:
        return None


def _manager(seed: _Seed) -> CurrentUser:
    return CurrentUser(employee_id=seed.manager.id, role=Role.MANAGER, manager_id=None)


async def test_structured_paste_never_reaches_the_model(db: AsyncSession, seed: _Seed) -> None:
    """The deterministic parser handles the format people actually paste, so a
    structured paste must not spend an LLM call even when one is configured."""
    llm = _StubParseLlm([("should not be used", None)])
    tasks = await _task_service(db, llm).parse_tasks(
        _manager(seed), "Remy -\n- Ship the thing\n- Fix the other thing\n"
    )
    assert llm.calls == 0
    assert [t.title for t in tasks] == ["Ship the thing", "Fix the other thing"]


async def test_unstructured_paste_falls_back_and_resolves_names_in_code(
    db: AsyncSession, seed: _Seed
) -> None:
    """Prose carries no header and no bullets, so the model is asked - and the
    name IT reports is matched to a person here, against the caller's roster."""
    llm = _StubParseLlm(
        [
            ("Own the migration", "Remy"),  # resolves to the manager's report
            ("Chase the vendor", "Olive"),  # out of the caller's scope
            ("Tidy the backlog", "General"),  # means nobody
        ]
    )
    tasks = await _task_service(db, llm).parse_tasks(
        _manager(seed),
        "In the meeting we agreed someone has to own the migration before Friday, "
        "chase the vendor, and tidy the backlog at some point.",
    )
    assert llm.calls == 1
    # The paste reached the model; the roster did not.
    assert str(seed.report.id) not in llm.texts[0]
    assert "Remy Report" not in llm.texts[0]

    resolved = {t.title: (t.assignee_id, t.assignee_name) for t in tasks}
    assert resolved["Own the migration"] == (seed.report.id, "Remy")
    # Out of scope resolves to nobody, but keeps the label for the user to fix.
    assert resolved["Chase the vendor"] == (None, "Olive")
    assert resolved["Tidy the backlog"] == (None, None)


async def test_a_failing_model_still_returns_whatever_could_be_read(
    db: AsyncSession, seed: _Seed
) -> None:
    """A bulleted paste is structured, so it never asks the model at all - but if
    the model is reached and fails, the line split is still better than an error
    the user cannot act on."""

    class _Failing(_StubParseLlm):
        async def parse_tasks(self, text: str) -> list[tuple[str, str | None]]:
            raise LlmError("provider down")

    with pytest.raises(ValidationError):
        # Nothing readable AND the model is down -> a real error, not silence.
        await _task_service(db, _Failing()).parse_tasks(_manager(seed), "-----\n=====\n")


async def test_a_manager_sees_their_whole_reporting_tree(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Max manages Remy, who leads a team of their own (Gia). Max must see
    Gia's tasks and profile too, not only Remy's; the outsider still sees none."""
    grandchild = Employee(
        hr_external_id="hr-grandchild",
        work_email="gia@corp.test",
        full_name="Gia Grandchild",
        role=Role.EMPLOYEE,
        manager_id=seed.report.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(grandchild)
    await db.commit()
    task = await _create_as(client, settings, seed.report, str(grandchild.id))

    def assignees(resp: object) -> set[str]:
        return {t["assignee_id"] for t in resp.json()["items"]}  # type: ignore[attr-defined]

    top = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.manager))
    opened = await client.get(
        f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.manager)
    )
    profile = await client.get(
        f"/api/v1/employees/{grandchild.id}", headers=auth_headers(settings, seed.manager)
    )
    outsider = await client.get(
        f"/api/v1/tasks/{task['id']}", headers=auth_headers(settings, seed.outsider)
    )
    peer_view = await client.get(
        f"/api/v1/employees/{seed.manager.id}", headers=auth_headers(settings, grandchild)
    )

    assert top.status_code == 200
    assert str(grandchild.id) in assignees(top)
    assert opened.status_code == 200
    assert profile.status_code == 200
    assert outsider.status_code == 404
    # Scope runs down the tree only: Gia cannot read up to her skip-level.
    assert peer_view.status_code in (403, 404)


async def test_a_reporting_cycle_does_not_hang_the_scope_query(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Bad HR data (A reports to B reports to A) must still answer."""
    seed.manager.manager_id = seed.report.id
    await db.commit()

    resp = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.manager))

    assert resp.status_code == 200
