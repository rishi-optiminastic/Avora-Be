"""Task lead - see every task and assign to anyone, with no HR access.

A Head of Business puts work on any department and the Founder's Office follows
everyone's task list. Neither may read salaries or attendance, so the grant must
widen tasks and nothing else, only an admin may hand it out, and a visiting task
lead must never be counted as this workspace's staff.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenancy import SHARED_SCHEMA
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceStatus
from tests.conftest import _Seed, auth_headers


def _task(assignee: Employee, title: str = "Plan the launch") -> dict[str, object]:
    return {"title": title, "assignee_id": str(assignee.id)}


async def _grant(client: AsyncClient, settings: Settings, seed: _Seed, who: Employee) -> None:
    res = await client.put(
        f"/api/v1/employees/{who.id}/org-wide-assign",
        json={"assigns_org_wide": True},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 200, res.text
    assert res.json()["assigns_org_wide"] is True


async def test_only_an_admin_hands_out_the_grant(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    for actor in (seed.manager, seed.report, hr):
        res = await client.put(
            f"/api/v1/employees/{seed.report.id}/org-wide-assign",
            json={"assigns_org_wide": True},
            headers=auth_headers(settings, actor),
        )
        assert res.status_code == 403, actor.work_email


async def test_a_task_lead_assigns_to_anyone_and_sees_every_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    lead = seed.report  # an executive with nobody reporting to them
    before = await client.post(
        "/api/v1/tasks", json=_task(seed.outsider), headers=auth_headers(settings, lead)
    )
    assert before.status_code == 403

    # Someone else's task, which the lead has no line to.
    others = await client.post(
        "/api/v1/tasks",
        json=_task(seed.manager, "Manager's own work"),
        headers=auth_headers(settings, seed.manager),
    )
    assert others.status_code == 201

    await _grant(client, settings, seed, lead)
    h = auth_headers(settings, lead)
    assigned = await client.post("/api/v1/tasks", json=_task(seed.outsider), headers=h)
    assert assigned.status_code == 201, assigned.text

    picker = await client.get("/api/v1/employees/assignable", headers=h)
    assert {p["id"] for p in picker.json()} >= {
        str(seed.outsider.id),
        str(seed.manager.id),
        str(seed.admin.id),
    }

    board = await client.get("/api/v1/tasks?size=100", headers=h)
    titles = {t["title"] for t in board.json()["items"]}
    assert {"Plan the launch", "Manager's own work"} <= titles


async def test_a_task_lead_manages_what_they_assigned_but_not_others_work(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    lead = seed.report
    await _grant(client, settings, seed, lead)
    h = auth_headers(settings, lead)
    mine = (await client.post("/api/v1/tasks", json=_task(seed.outsider), headers=h)).json()
    fixed = await client.patch(
        f"/api/v1/tasks/{mine['id']}", json={"title": "Plan the launch, v2"}, headers=h
    )
    assert fixed.status_code == 200, fixed.text

    theirs = (
        await client.post(
            "/api/v1/tasks",
            json=_task(seed.manager, "Not yours"),
            headers=auth_headers(settings, seed.manager),
        )
    ).json()
    refused = await client.patch(
        f"/api/v1/tasks/{theirs['id']}", json={"title": "Renamed"}, headers=h
    )
    assert refused.status_code == 403


async def test_the_grant_reaches_no_hr_data(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    lead = seed.report
    await _grant(client, settings, seed, lead)
    salary = await client.get(
        f"/api/v1/employees/{seed.outsider.id}/compensation", headers=auth_headers(settings, lead)
    )
    assert salary.status_code in (403, 404)


async def test_a_visiting_task_lead_is_not_this_workspace_s_staff(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    db.add(
        Workspace(
            slug=settings.primary_workspace_slug,
            schema_name=SHARED_SCHEMA,
            name="Primary",
            location="",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=datetime.now(UTC),
        )
    )
    await db.commit()
    res = await client.post(
        "/api/v1/workspaces/guests",
        json={"email": "head@hq-group.com", "role": "task_lead", "name": "Head Of Business"},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["role"] == "executive"
    assert body["assigns_org_wide"] is True and body["is_guest"] is True


async def test_the_follow_up_list_holds_only_what_you_assigned(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    lead = seed.report
    await _grant(client, settings, seed, lead)
    h = auth_headers(settings, lead)
    await client.post("/api/v1/tasks", json=_task(seed.outsider, "Mine to chase"), headers=h)
    await client.post(
        "/api/v1/tasks",
        json=_task(seed.manager, "Someone else's"),
        headers=auth_headers(settings, seed.manager),
    )
    mine = await client.get("/api/v1/tasks?assigned_by_me=true&size=100", headers=h)
    assert [t["title"] for t in mine.json()["items"]] == ["Mine to chase"]


async def test_a_manager_task_lead_edits_only_their_own_people(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The manager role alone used to mean "runs whoever this task is on",
    because a manager could only see their team's tasks. A task lead sees all of
    them, so the role must no longer unlock editing someone else's work."""
    await _grant(client, settings, seed, seed.manager)
    theirs = await client.post(
        "/api/v1/tasks",
        json=_task(seed.outsider, "Admin's ask"),
        headers=auth_headers(settings, seed.admin),
    )
    task_id = theirs.json()["id"]
    lead = auth_headers(settings, seed.manager)
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=lead)).status_code == 200
    res = await client.patch(f"/api/v1/tasks/{task_id}", json={"title": "Mine now"}, headers=lead)
    assert res.status_code == 403, res.text


async def test_a_task_lead_adds_anyone_on_staff_to_their_task(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _grant(client, settings, seed, seed.report)
    h = auth_headers(settings, seed.report)
    made = await client.post("/api/v1/tasks", json=_task(seed.manager, "Joint"), headers=h)
    res = await client.post(
        f"/api/v1/tasks/{made.json()['id']}/collaborators",
        json={"employee_id": str(seed.outsider.id)},
        headers=h,
    )
    assert res.status_code in (200, 201), res.text
