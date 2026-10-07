"""Automatic overdue-task escalation: tiers, idempotency, and who gets pulled in.

Escalation ADDS PEOPLE to tasks, so the thing that matters most here is that a
sweep run repeatedly never re-notifies or re-adds anyone - a worker ticks hourly
forever, and a tier that fired twice would be noise nobody could switch off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmployeeStatus, Role
from app.models.notification import Notification, NotificationKind
from app.models.task import Task, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.task import TaskRepository
from app.services.notification_service import NotificationService
from app.services.task_escalation_service import TaskEscalationService
from tests.conftest import _Seed

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _service(db: AsyncSession) -> TaskEscalationService:
    return TaskEscalationService(
        TaskRepository(db),
        EmployeeRepository(db),
        NotificationService(NotificationRepository(db)),
        AuditRepository(db),
    )


async def _task(
    db: AsyncSession,
    seed: _Seed,
    *,
    days_overdue: float,
    status: TaskStatus = TaskStatus.TODO,
) -> Task:
    task = Task(
        title=f"Ship the thing ({days_overdue}d)",
        assignee_id=seed.report.id,
        assigned_by_id=seed.manager.id,
        due_date=NOW - timedelta(days=days_overdue),
        status=status,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _notifications_for(db: AsyncSession, employee_id: object) -> list[Notification]:
    rows = await db.execute(
        select(Notification).where(
            Notification.recipient_id == employee_id,
            Notification.kind == NotificationKind.ESCALATION,
        )
    )
    return list(rows.scalars().all())


async def test_a_task_not_yet_overdue_is_left_alone(db: AsyncSession, seed: _Seed) -> None:
    task = await _task(db, seed, days_overdue=-2)  # due in two days
    await _service(db).run_due(NOW)
    await db.refresh(task)
    assert task.escalation_level == 0
    assert task.escalated is False
    assert await _notifications_for(db, seed.report.id) == []


async def test_one_day_overdue_warns_only_the_assignee(db: AsyncSession, seed: _Seed) -> None:
    task = await _task(db, seed, days_overdue=1.5)
    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)

    assert outcome.assignee_warned == 1
    assert outcome.managers_added == 0
    assert task.escalation_level == 1
    assert len(await _notifications_for(db, seed.report.id)) == 1
    # The manager is NOT pulled in this early - that is the whole point of tiers.
    assert await _notifications_for(db, seed.manager.id) == []
    await db.refresh(task, attribute_names=["collaborators"])
    assert task.collaborators == []


async def test_three_days_overdue_adds_the_reporting_manager(db: AsyncSession, seed: _Seed) -> None:
    task = await _task(db, seed, days_overdue=4)
    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])

    assert outcome.managers_added == 1
    assert task.escalation_level == 2
    assert [c.id for c in task.collaborators] == [seed.manager.id]
    assert len(await _notifications_for(db, seed.manager.id)) == 1


async def test_six_days_overdue_also_adds_an_admin(db: AsyncSession, seed: _Seed) -> None:
    task = await _task(db, seed, days_overdue=7)
    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])

    assert outcome.admins_added == 1
    assert task.escalation_level == 3
    ids = {c.id for c in task.collaborators}
    assert ids == {seed.manager.id, seed.admin.id}
    assert len(await _notifications_for(db, seed.admin.id)) == 1


async def test_running_the_sweep_again_changes_nothing(db: AsyncSession, seed: _Seed) -> None:
    """The worker ticks hourly forever. A tier that fired twice would spam people
    with no way to turn it off, so idempotency is the load-bearing property."""
    task = await _task(db, seed, days_overdue=7)
    first = await _service(db).run_due(NOW)
    assert first.total == 3

    for _ in range(5):
        again = await _service(db).run_due(NOW + timedelta(hours=1))
        assert again.total == 0

    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])
    assert task.escalation_level == 3
    assert len(task.collaborators) == 2  # not re-added
    assert len(await _notifications_for(db, seed.report.id)) == 1
    assert len(await _notifications_for(db, seed.manager.id)) == 1
    assert len(await _notifications_for(db, seed.admin.id)) == 1


async def test_completing_a_task_stops_it_escalating(db: AsyncSession, seed: _Seed) -> None:
    task = await _task(db, seed, days_overdue=7, status=TaskStatus.DONE)
    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)
    assert outcome.total == 0
    assert task.escalation_level == 0


async def test_an_assignee_with_no_manager_still_reaches_the_admin_tier(
    db: AsyncSession, seed: _Seed
) -> None:
    """A missing reporting manager must not stall escalation forever. Without
    advancing the level on the empty tier, the task would be re-examined on every
    sweep and never reach an admin."""
    task = await _task(db, seed, days_overdue=7)
    task.assignee_id = seed.outsider.id  # reports to nobody
    await db.commit()

    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])

    assert outcome.managers_added == 0
    assert outcome.admins_added == 1
    assert task.escalation_level == 3
    assert [c.id for c in task.collaborators] == [seed.admin.id]


async def test_a_task_assigned_to_the_admin_does_not_collaborate_them_onto_it(
    db: AsyncSession, seed: _Seed
) -> None:
    """Nobody is added as a collaborator on their own task - it would tell them
    they have been pulled in to supervise themselves."""
    task = await _task(db, seed, days_overdue=7)
    task.assignee_id = seed.admin.id
    await db.commit()

    await _service(db).run_due(NOW)
    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])

    assert task.collaborators == []
    assert task.escalation_level == 3  # still advanced, so it stops being re-examined


async def test_an_inactive_manager_is_never_pulled_in(db: AsyncSession, seed: _Seed) -> None:
    """Offboarded people keep their rows (soft delete), so the sweep must check
    `is_active` or it would escalate to someone who has left."""
    lead = Employee(
        hr_external_id="hr-gone",
        work_email="gone@corp.test",
        full_name="Gus Gone",
        role=Role.MANAGER,
        status=EmployeeStatus.INACTIVE,
        is_active=False,
    )
    db.add(lead)
    await db.flush()
    worker = Employee(
        hr_external_id="hr-worker",
        work_email="worker@corp.test",
        full_name="Wen Worker",
        role=Role.EMPLOYEE,
        manager_id=lead.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(worker)
    await db.commit()

    task = await _task(db, seed, days_overdue=4)
    task.assignee_id = worker.id
    await db.commit()

    await _service(db).run_due(NOW)
    await db.refresh(task, attribute_names=["collaborators"])
    assert [c.id for c in task.collaborators] == []
    assert await _notifications_for(db, lead.id) == []


async def test_me_reports_leading_a_team_from_the_reporting_line_not_the_role(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """`/me` must say "you lead a team" from `manager_id`, not from the role, or
    a lead carrying the EMPLOYEE role gets a personal view of data that is
    already theirs to manage."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)

    lead = Employee(
        hr_external_id="hr-quiet-lead",
        work_email="quiet.lead@corp.test",
        full_name="Lee Quiet",
        role=Role.EMPLOYEE,  # runs a team without the title
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(lead)
    await db.flush()
    db.add(
        Employee(
            hr_external_id="hr-quiet-report",
            work_email="quiet.report@corp.test",
            full_name="Ray Report",
            role=Role.EMPLOYEE,
            manager_id=lead.id,
            status=EmployeeStatus.ACTIVE,
            is_active=True,
        )
    )
    await db.commit()

    mine = await client.get("/api/v1/employees/me", headers=auth_headers(settings, lead))
    assert mine.status_code == 200, mine.text
    assert mine.json()["leads_team"] is True

    # Someone with nobody reporting to them is not offered the team views.
    solo = await client.get("/api/v1/employees/me", headers=auth_headers(settings, seed.outsider))
    assert solo.json()["leads_team"] is False


async def _lead_with_report(db: AsyncSession, tag: str) -> tuple[Employee, Employee]:
    lead = Employee(
        hr_external_id=f"hr-lead-{tag}",
        work_email=f"lead.{tag}@corp.test",
        full_name=f"Lead {tag}",
        role=Role.EMPLOYEE,  # runs a team without the title
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(lead)
    await db.flush()
    report = Employee(
        hr_external_id=f"hr-rep-{tag}",
        work_email=f"rep.{tag}@corp.test",
        full_name=f"Report {tag}",
        role=Role.EMPLOYEE,
        manager_id=lead.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(report)
    await db.commit()
    return lead, report


async def test_a_team_lead_without_the_title_can_assign_to_their_report(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """The team view is only as good as the roster behind it.

    `list_assignable` and `_can_assign_to` both gated on the MANAGER role, so a
    lead carrying the EMPLOYEE role got an empty assignee picker and an empty
    team view - of the very reports the server already lets them read.
    """
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)
    lead, report = await _lead_with_report(db, "assign")

    roster = await client.get("/api/v1/employees/assignable", headers=auth_headers(settings, lead))
    assert roster.status_code == 200, roster.text
    assert str(report.id) in [e["id"] for e in roster.json()]

    created = await client.post(
        "/api/v1/tasks",
        json={"title": "Fix the thing", "assignee_id": str(report.id)},
        headers=auth_headers(settings, lead),
    )
    assert created.status_code == 201, created.text
    assert created.json()["assignee_id"] == str(report.id)


async def test_assignment_scope_is_not_a_free_pass(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """Relationship-based does not mean unrestricted: a lead still has no say
    over somebody who does not report to them."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)
    lead, _ = await _lead_with_report(db, "nopass")

    resp = await client.post(
        "/api/v1/tasks",
        json={"title": "Not yours", "assignee_id": str(seed.report.id)},
        headers=auth_headers(settings, lead),
    )
    assert resp.status_code in (403, 404)


async def test_a_viewer_cannot_assign_to_someone_on_their_reporting_line(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """VIEWER is scoped to itself, so dropping the role check must not hand it
    an assignment edge via the org chart."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)
    viewer = Employee(
        hr_external_id="hr-viewer-assign",
        work_email="viewer.assign@corp.test",
        full_name="Vic Viewer",
        role=Role.VIEWER,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(viewer)
    await db.flush()
    intern = Employee(
        hr_external_id="hr-intern-assign",
        work_email="intern.assign@corp.test",
        full_name="Ira Intern",
        role=Role.EMPLOYEE,
        manager_id=viewer.id,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(intern)
    await db.commit()

    resp = await client.post(
        "/api/v1/tasks",
        json={"title": "No", "assignee_id": str(intern.id)},
        headers=auth_headers(settings, viewer),
    )
    assert resp.status_code in (403, 404)


async def test_an_empty_tier_advances_but_does_not_claim_an_escalation(
    db: AsyncSession, seed: _Seed
) -> None:
    """`escalation_level` must advance so the tier stops being re-examined, but
    the board's "Escalated · manager" badge is a claim that somebody was actually
    added - and on a task with no manager and no admin, nobody was."""
    from sqlalchemy import delete

    await db.execute(delete(Employee).where(Employee.role == Role.ADMIN))
    task = await _task(db, seed, days_overdue=7)
    task.assignee_id = seed.outsider.id  # reports to nobody
    await db.commit()

    outcome = await _service(db).run_due(NOW)
    await db.refresh(task)
    await db.refresh(task, attribute_names=["collaborators"])

    assert outcome.managers_added == 0
    assert outcome.admins_added == 0
    assert task.escalation_level == 3  # advanced, so it stops being re-examined
    assert task.collaborators == []
    assert task.escalated is False  # nobody was pulled in, so nothing to badge


async def test_a_team_lead_sees_and_acts_on_their_reports_tasks(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """The team overview is only as good as the task scope behind it.

    `TaskRepository._scope_clause` gated the direct-reports branch on the MANAGER
    role, so a lead carrying the EMPLOYEE role saw none of their team's work -
    the overview listed every report with zero tasks - and `escalate` refused
    outright. Scope and authority both follow the reporting edge now.
    """
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)
    lead, report = await _lead_with_report(db, "scope")

    task = Task(
        title="Report's work",
        assignee_id=report.id,
        assigned_by_id=report.id,
        status=TaskStatus.TODO,
    )
    db.add(task)
    await db.commit()

    listed = await client.get("/api/v1/tasks", headers=auth_headers(settings, lead))
    assert listed.status_code == 200, listed.text
    assert str(task.id) in [t["id"] for t in listed.json()["items"]]

    escalated = await client.post(
        f"/api/v1/tasks/{task.id}/escalate", headers=auth_headers(settings, lead)
    )
    assert escalated.status_code == 200, escalated.text
    assert escalated.json()["escalated"] is True


async def test_a_collaborator_cannot_escalate_someone_elses_task(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """Scope includes tasks you merely collaborate on, so authority must be the
    NARROWER test - otherwise the escalation sweep adding someone as a
    collaborator would hand them authority over the assignee."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)
    task = Task(
        title="Not theirs to escalate",
        assignee_id=seed.report.id,
        assigned_by_id=seed.manager.id,
        status=TaskStatus.TODO,
    )
    db.add(task)
    await db.commit()
    await TaskRepository(db).add_collaborator(task.id, seed.outsider.id)
    await db.commit()

    seen = await client.get("/api/v1/tasks", headers=auth_headers(settings, seed.outsider))
    assert str(task.id) in [t["id"] for t in seen.json()["items"]]  # in scope...

    resp = await client.post(
        f"/api/v1/tasks/{task.id}/escalate", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 403  # ...but not theirs to escalate


async def test_a_long_title_still_notifies(db: AsyncSession, seed: _Seed) -> None:
    """`Notification.title` is String(160), `Task.title` is String(256).

    An over-long title made the notification INSERT fail; `create_isolated`
    swallows that in a SAVEPOINT and returns None, and the sweep advanced the
    tier anyway - so the assignee was never warned and tier 1 could never
    re-fire. SQLite ignores VARCHAR widths, which is why nothing caught it.
    """
    task = await _task(db, seed, days_overdue=2)
    task.title = "L" * 256
    await db.commit()

    await _service(db).run_due(NOW)
    notes = await _notifications_for(db, seed.report.id)
    assert len(notes) == 1
    assert len(notes[0].title) <= 160


async def test_one_sweep_is_bounded(db: AsyncSession, seed: _Seed) -> None:
    """The first sweep after switching this on walks the whole backlog, and an
    ESCALATION is rendered as a blocking full-screen modal - uncapped, one person
    would be handed a modal per stale task. The remainder waits for the next tick."""
    for i in range(7):
        t = await _task(db, seed, days_overdue=2)
        t.title = f"backlog {i}"
    await db.commit()

    service = TaskEscalationService(
        TaskRepository(db),
        EmployeeRepository(db),
        NotificationService(NotificationRepository(db)),
        AuditRepository(db),
        batch_limit=3,
    )
    first = await service.run_due(NOW)
    assert first.assignee_warned == 3  # capped, not 7
    assert len(await _notifications_for(db, seed.report.id)) == 3

    second = await service.run_due(NOW)
    assert second.assignee_warned == 3  # the next tick picks up where it left off


async def test_pushing_the_deadline_out_lets_a_task_escalate_again(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """A moved deadline is a fresh deadline. `escalation_level` was never reset,
    so a re-planned task could never escalate again however late it ran - and the
    board kept badging it "Escalated" for a deadline that no longer existed."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)

    task = await _task(db, seed, days_overdue=7)
    await _service(db).run_due(NOW)
    await db.commit()
    await db.refresh(task)
    assert task.escalation_level == 3

    moved = await client.patch(
        f"/api/v1/tasks/{task.id}",
        json={"due_date": (NOW + timedelta(days=30)).isoformat()},
        headers=auth_headers(settings, seed.manager),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["escalated"] is False
    assert moved.json()["escalation_level"] == 0


async def test_pulling_a_deadline_IN_does_not_clear_a_live_escalation(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """Only a date moved FORWARD is a re-plan. Bringing one in makes the task
    more urgent, not less, so it must not wipe the escalation already raised."""
    from httpx import AsyncClient

    from app.core.config import Settings
    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    assert isinstance(settings, Settings)

    task = await _task(db, seed, days_overdue=7)
    await _service(db).run_due(NOW)
    await db.commit()

    moved = await client.patch(
        f"/api/v1/tasks/{task.id}",
        json={"due_date": (NOW - timedelta(days=9)).isoformat()},
        headers=auth_headers(settings, seed.manager),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["escalation_level"] == 3


async def test_the_worker_does_nothing_while_disabled(db: AsyncSession, seed: _Seed) -> None:
    """`task_escalation_enabled` is the only thing between "off by default" and a
    sweep that adds people to every stale task in the org, and nothing exercised
    it. The worker must no-op when the flag is off, whatever is overdue."""
    from worker import task_escalation_scheduler as scheduler

    from app.core.config import get_settings

    await _task(db, seed, days_overdue=7)
    settings = get_settings()
    assert settings.task_escalation_enabled is False  # the shipped default

    await scheduler._tick()

    # Nothing escalated, and nobody was notified.
    assert await _notifications_for(db, seed.report.id) == []
    assert await _notifications_for(db, seed.manager.id) == []
