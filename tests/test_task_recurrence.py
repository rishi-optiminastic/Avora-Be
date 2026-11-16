"""Regenerating recurring tasks: idempotency, what copies, and what stops it.

This sweep CREATES work, so the thing that matters most is that running it
repeatedly creates nothing extra - a worker ticks hourly forever, and a series
that generated twice an hour would bury the person it belongs to.

The other half is `_next_instance`: a recurring task must arrive as the
agreement and nothing about the last time it ran.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.recurrence import WorkingCalendar
from app.models.employee import Employee
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import LeaveRepository
from app.repositories.task import TaskRepository
from app.services.task_recurrence_service import TaskRecurrenceService
from tests.conftest import _Seed

TODAY = date(2026, 11, 12)  # a Thursday
WEEKDAYS = frozenset({0, 1, 2, 3, 4})
OPEN = WorkingCalendar(weekdays=WEEKDAYS)


def _service(db: AsyncSession) -> TaskRecurrenceService:
    return TaskRecurrenceService(TaskRepository(db), EmployeeRepository(db), AuditRepository(db))


async def _task(
    db: AsyncSession,
    seed: _Seed,
    *,
    cadence: TaskCadence = TaskCadence.DAILY,
    due: date | None = date(2026, 11, 11),
    status: TaskStatus = TaskStatus.TODO,
    **extra: object,
) -> Task:
    task = Task(
        title="Submit the daily report",
        assignee_id=seed.report.id,
        assigned_by_id=seed.manager.id,
        cadence=cadence,
        due_date=(
            datetime.combine(due, datetime.min.time(), tzinfo=UTC) if due is not None else None
        ),
        status=status,
        **extra,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(Task)) or 0)


# --------------------------------------------------------------------------- #
# Idempotency - the rule that makes an hourly sweep safe
# --------------------------------------------------------------------------- #
async def test_a_sweep_creates_the_next_instance(db: AsyncSession, seed: _Seed) -> None:
    await _task(db, seed)

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 1
    assert await _count(db) == 2


async def test_running_it_again_creates_nothing(db: AsyncSession, seed: _Seed) -> None:
    """THE test. The worker ticks hourly forever; a second instance an hour
    would bury the person it belongs to."""
    await _task(db, seed)
    await _service(db).run_due(TODAY)
    await db.commit()

    for _ in range(5):
        outcome = await _service(db).run_due(TODAY)
        await db.commit()
        assert outcome.created == 0
        assert outcome.already_current == 1

    assert await _count(db) == 2


async def test_a_week_of_downtime_produces_one_task_not_seven(
    db: AsyncSession, seed: _Seed
) -> None:
    """Switching the worker back on after an outage must not dump a backlog."""
    await _task(db, seed, due=TODAY - timedelta(days=7))

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 1
    assert await _count(db) == 2


async def test_nothing_is_ever_created_already_overdue(db: AsyncSession, seed: _Seed) -> None:
    """Switching on against a year of history creates tomorrow's task, not
    last January's."""
    await _task(db, seed, cadence=TaskCadence.MONTHLY, due=date(2026, 1, 15))

    await _service(db).run_due(TODAY)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() >= TODAY


async def test_the_newest_instance_seeds_the_next_not_an_old_one(
    db: AsyncSession, seed: _Seed
) -> None:
    """Seeding from history would fork the series into two parallel chains."""
    first = await _task(db, seed, due=date(2026, 11, 9), status=TaskStatus.DONE)
    await _task(db, seed, due=date(2026, 11, 11), series_id=first.id)

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 1, "one series, one new instance"
    assert await _count(db) == 3


# --------------------------------------------------------------------------- #
# What copies forward, and what must not
# --------------------------------------------------------------------------- #
async def test_the_agreement_copies_forward(db: AsyncSession, seed: _Seed) -> None:
    await _task(
        db,
        seed,
        description="Numbers for yesterday.",
        priority=TaskPriority.HIGH,
        expected_output="A table in the channel",
    )

    await _service(db).run_due(TODAY)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.title == "Submit the daily report"
    assert fresh.description == "Numbers for yesterday."
    assert fresh.assignee_id == seed.report.id
    assert fresh.priority is TaskPriority.HIGH
    assert fresh.expected_output == "A table in the channel"
    assert fresh.cadence is TaskCadence.DAILY


async def test_last_weeks_result_does_NOT_copy_forward(db: AsyncSession, seed: _Seed) -> None:
    """Monday's task arriving pre-filled with Friday's result is a lie about
    work nobody has done - the same one the probation checklist was fixed to
    stop telling."""
    await _task(
        db,
        seed,
        status=TaskStatus.DONE,
        completion_pct=100,
        final_outcome="Sent, 42 rows.",
        review_notes="Good.",
        remarks="Took an hour.",
        blocked_reason="Was waiting on data",
        escalated=True,
        escalation_level=3,
        completed_at=datetime(2026, 11, 11, 17, 0, tzinfo=UTC),
    )

    await _service(db).run_due(TODAY)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.status is TaskStatus.TODO
    assert fresh.completion_pct == 0
    assert fresh.final_outcome is None
    assert fresh.review_notes is None
    assert fresh.remarks is None
    assert fresh.blocked_reason is None
    assert fresh.completed_at is None
    assert fresh.escalated is False
    assert fresh.escalation_level == 0, "last week being late says nothing about this week"


async def test_a_completed_task_still_regenerates(db: AsyncSession, seed: _Seed) -> None:
    """Completing Monday's is exactly when Tuesday's should appear."""
    await _task(db, seed, status=TaskStatus.DONE)

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 1


# --------------------------------------------------------------------------- #
# What stops a series
# --------------------------------------------------------------------------- #
async def test_a_one_time_task_never_recurs(db: AsyncSession, seed: _Seed) -> None:
    await _task(db, seed, cadence=TaskCadence.ONE_TIME)

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 0
    assert await _count(db) == 1


async def test_a_paused_series_generates_nothing(db: AsyncSession, seed: _Seed) -> None:
    await _task(db, seed, recurrence_active=False)

    assert (await _service(db).run_due(TODAY)).created == 0


async def test_a_series_past_its_end_date_stops_itself(db: AsyncSession, seed: _Seed) -> None:
    """Ended, not deleted: the completed history stays."""
    task = await _task(db, seed, recurs_until=date(2026, 11, 11))

    outcome = await _service(db).run_due(TODAY)
    await db.commit()
    await db.refresh(task)

    assert outcome.created == 0
    assert task.recurrence_active is False
    assert await _count(db) == 1, "and nothing was deleted"


async def test_a_series_whose_assignee_left_stops_itself(db: AsyncSession, seed: _Seed) -> None:
    """Generating work nobody holds is worse than generating nothing."""
    task = await _task(db, seed)
    seed.report.is_active = False
    await db.commit()

    outcome = await _service(db).run_due(TODAY)
    await db.commit()
    await db.refresh(task)

    assert outcome.created == 0
    assert task.recurrence_active is False
    assert "assignee_gone" in outcome.stopped[0]


async def test_a_daily_task_skips_the_weekend(db: AsyncSession, seed: _Seed) -> None:
    """Friday 13th -> Monday 16th. A daily task on a Sunday is noise.

    `today` has to be AFTER the existing due date, or the idempotency check
    correctly refuses: that day is already covered.
    """
    await _task(db, seed, due=date(2026, 11, 13))  # Friday

    await _service(db).run_due(date(2026, 11, 14), OPEN)  # Saturday
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 16)


async def test_a_weekly_task_keeps_its_day_even_on_a_weekend(db: AsyncSession, seed: _Seed) -> None:
    """Its day IS the agreement. A Saturday weekly task means Saturday."""
    await _task(db, seed, cadence=TaskCadence.WEEKLY, due=date(2026, 11, 14))  # Saturday

    await _service(db).run_due(date(2026, 11, 15), OPEN)  # the Sunday after
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 21)


async def test_every_instance_shares_one_series_id(db: AsyncSession, seed: _Seed) -> None:
    """Including the first, which predates this feature and has a null one -
    it becomes the head of its own series rather than being rewritten."""
    first = await _task(db, seed)

    await _service(db).run_due(TODAY)
    await db.commit()
    await _service(db).run_due(TODAY + timedelta(days=1))
    await db.commit()

    rows = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).all()
    assert len(rows) == 2
    assert {r.series_id for r in rows} == {first.id}
    assert first.series_id is None, "the original row is not rewritten"


# --------------------------------------------------------------------------- #
# Who may stop a series (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
async def test_an_assignee_cannot_pause_their_own_series(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    """Pausing a series is a scheduling decision, not progress reporting.

    An assignee can report status and remarks on their own task; switching
    off the daily report everybody depends on is their manager's call.
    """
    from httpx import AsyncClient

    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    task = await _task(db, seed)

    resp = await client.patch(
        f"/api/v1/tasks/{task.id}",
        json={"recurrence_active": False},
        headers=auth_headers(settings, seed.report),  # type: ignore[arg-type]
    )

    assert resp.status_code == 403, resp.text
    await db.refresh(task)
    assert task.recurrence_active is True


async def test_the_manager_can_pause_it(
    client: object, settings: object, db: AsyncSession, seed: _Seed
) -> None:
    from httpx import AsyncClient

    from tests.conftest import auth_headers

    assert isinstance(client, AsyncClient)
    task = await _task(db, seed)

    resp = await client.patch(
        f"/api/v1/tasks/{task.id}",
        json={"recurrence_active": False},
        headers=auth_headers(settings, seed.manager),  # type: ignore[arg-type]
    )

    assert resp.status_code == 200, resp.text
    await db.refresh(task)
    assert task.recurrence_active is False
    assert (await _service(db).run_due(TODAY)).created == 0, "and it stops generating"


async def test_the_new_due_date_is_timezone_aware(db: AsyncSession, seed: _Seed) -> None:
    """Asserted on the object the service BUILDS, not on one read back.

    SQLite strips tzinfo on a round-trip, so a database check here would
    pass whatever the service did. A naive datetime is interpreted in the
    server's timezone by Postgres, which stored a task due the 12th as 18:30
    on the 11th and read it back a whole day early - found by running the
    sweep against real Postgres, not by this suite.
    """
    task = await _task(db, seed)

    fresh = TaskRecurrenceService._next_instance(task, task.id, date(2026, 11, 12))

    assert fresh.due_date is not None
    assert fresh.due_date.tzinfo is not None
    assert fresh.due_date.date() == date(2026, 11, 12)


# --------------------------------------------------------------------------- #
# Refinements
# --------------------------------------------------------------------------- #
async def test_the_start_date_moves_with_the_due_date(db: AsyncSession, seed: _Seed) -> None:
    """A task that starts two days before it is due keeps that gap.

    Copying the original start date unchanged made every instance's start
    one cadence step staler than the last, until a daily task claimed to
    have been open for months.
    """
    await _task(
        db,
        seed,
        due=date(2026, 11, 11),
        start_date=datetime(2026, 11, 9, tzinfo=UTC),
    )

    await _service(db).run_due(TODAY)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None and fresh.due_date.date() == date(2026, 11, 12)
    assert fresh.start_date is not None
    assert fresh.start_date.date() == date(2026, 11, 10), "the same two-day gap"


async def test_a_task_with_no_start_date_does_not_acquire_one(
    db: AsyncSession, seed: _Seed
) -> None:
    await _task(db, seed)

    await _service(db).run_due(TODAY)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.start_date is None


async def test_a_daily_task_skips_a_public_holiday(db: AsyncSession, seed: _Seed) -> None:
    """Diwali is as much a non-working day as a Sunday."""
    await _task(db, seed, due=date(2026, 11, 11))
    shut = WorkingCalendar(weekdays=WEEKDAYS, closed=frozenset({date(2026, 11, 12)}))

    await _service(db).run_due(TODAY, shut)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 13)


async def test_a_recurring_task_with_no_due_date_is_counted_not_guessed(
    db: AsyncSession, seed: _Seed
) -> None:
    """391 of the 509 live recurring tasks are in this state.

    There is nothing to step from, so it cannot generate - and inventing a
    date would create hundreds of tasks out of a guess. Counting it is what
    stops this being the same silent no-op that made `cadence` meaningless
    for years in the first place.
    """
    await _task(db, seed, due=None)

    outcome = await _service(db).run_due(TODAY)
    await db.commit()

    assert outcome.created == 0
    assert outcome.needs_a_due_date == 1
    assert await _count(db) == 1, "and nothing was invented"


async def test_a_one_time_task_with_no_due_date_is_not_counted(
    db: AsyncSession, seed: _Seed
) -> None:
    """It was never going to recur. Counting it would inflate a number
    somebody is meant to act on."""
    await _task(db, seed, cadence=TaskCadence.ONE_TIME, due=None)

    assert (await _service(db).run_due(TODAY)).needs_a_due_date == 0


async def test_a_paused_series_with_no_due_date_is_not_counted_either(
    db: AsyncSession, seed: _Seed
) -> None:
    """Nobody needs to go and set a date on a series that is switched off."""
    await _task(db, seed, due=None, recurrence_active=False)

    assert (await _service(db).run_due(TODAY)).needs_a_due_date == 0


# --------------------------------------------------------------------------- #
# Leave
#
# Five daily tasks generated across somebody's week off all go overdue,
# escalate to their manager, then to an admin. That is noise the system
# created for itself, so the sweep skips days its assignee is away.
# --------------------------------------------------------------------------- #
async def _approve_leave(
    db: AsyncSession, employee: Employee, start: date, end: date, kind: LeaveType
) -> None:
    db.add(
        Leave(
            employee_id=employee.id,
            leave_type=kind,
            start_date=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            end_date=datetime.combine(end, datetime.min.time(), tzinfo=UTC),
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()


def _with_leave(db: AsyncSession) -> TaskRecurrenceService:
    return TaskRecurrenceService(
        TaskRepository(db), EmployeeRepository(db), AuditRepository(db), LeaveRepository(db)
    )


async def test_a_daily_task_skips_a_day_its_assignee_is_on_leave(
    db: AsyncSession, seed: _Seed
) -> None:
    await _task(db, seed, due=date(2026, 11, 11))
    await _approve_leave(db, seed.report, date(2026, 11, 12), date(2026, 11, 12), LeaveType.PLANNED)

    await _with_leave(db).run_due(TODAY, OPEN)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 13)


async def test_a_week_of_leave_does_not_produce_five_tasks(db: AsyncSession, seed: _Seed) -> None:
    """The failure this exists to prevent."""
    await _task(db, seed, due=date(2026, 11, 11))
    await _approve_leave(db, seed.report, date(2026, 11, 12), date(2026, 11, 18), LeaveType.ANNUAL)

    outcome = await _with_leave(db).run_due(TODAY, OPEN)
    await db.commit()

    assert outcome.created == 1, "one task, waiting for them when they are back"
    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 19)


async def test_a_half_day_does_not_block_the_task(db: AsyncSession, seed: _Seed) -> None:
    """They are in for half the day. Skipping their work because they left
    at lunch would be wrong."""
    await _task(db, seed, due=date(2026, 11, 11))
    await _approve_leave(
        db, seed.report, date(2026, 11, 12), date(2026, 11, 12), LeaveType.HALF_DAY
    )

    await _with_leave(db).run_due(TODAY, OPEN)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 12)


async def test_unapproved_leave_does_not_block_anything(db: AsyncSession, seed: _Seed) -> None:
    """A request nobody has decided on is not time off yet, and acting on it
    would let anyone cancel their own recurring work by asking."""
    await _task(db, seed, due=date(2026, 11, 11))
    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.PLANNED,
            start_date=datetime(2026, 11, 12, tzinfo=UTC),
            end_date=datetime(2026, 11, 12, tzinfo=UTC),
            status=LeaveStatus.SUBMITTED,
        )
    )
    await db.commit()

    await _with_leave(db).run_due(TODAY, OPEN)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 12)


async def test_one_persons_leave_does_not_move_another_persons_task(
    db: AsyncSession, seed: _Seed
) -> None:
    """The calendar is laid over PER ASSIGNEE. Sharing one would let a single
    person's holiday pause the whole company's recurring work."""
    await _task(db, seed, due=date(2026, 11, 11))
    await _approve_leave(db, seed.manager, date(2026, 11, 12), date(2026, 11, 18), LeaveType.ANNUAL)

    await _with_leave(db).run_due(TODAY, OPEN)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 12), "the report is not on leave"


async def test_a_weekly_task_is_not_moved_by_leave(db: AsyncSession, seed: _Seed) -> None:
    """Its day is the agreement. Moving "the Monday review" because one
    person is away changes what it means for everyone else on it."""
    await _task(db, seed, cadence=TaskCadence.WEEKLY, due=date(2026, 11, 9))
    await _approve_leave(db, seed.report, date(2026, 11, 16), date(2026, 11, 16), LeaveType.PLANNED)

    await _with_leave(db).run_due(date(2026, 11, 10), OPEN)
    await db.commit()

    fresh = (await db.scalars(select(Task).where(Task.series_id.is_not(None)))).one()
    assert fresh.due_date is not None
    assert fresh.due_date.date() == date(2026, 11, 16)
