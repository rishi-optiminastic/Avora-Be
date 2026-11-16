"""Regenerating recurring tasks - the rule, with no I/O objects in the rules.

`Task.cadence` was a label nothing read: 509 live tasks say DAILY, WEEKLY or
MONTHLY and none ever produced a second instance, so people re-created the
work by hand. This sweep closes that.

What makes it safe to run on a schedule:

* **Idempotent.** For each series, if an instance is already due on or after
  today, nothing is created. Running hourly, or re-running after the worker
  was down for a week, produces exactly ONE task - not twenty-four, not seven.
* **Never retroactive.** `next_due` rolls forward to today, so switching this
  on against a year of history creates tomorrow's task, not last January's.
* **Only the newest instance of a series seeds the next.** An older one is
  history; seeding from it would fork the series.
* **A gone assignee stops the series** rather than generating work nobody
  holds, and says so once.
* **A daily task skips days its assignee is on approved leave.** Five tasks
  nobody can do all go overdue, escalate to their manager and then to an
  admin - noise the system created for itself. Half-days do not count: the
  person is in.

What copies forward, and what must NOT: see `_next_instance`. Carrying an
outcome or a completion forward would mean Monday's task arrives pre-filled
with Friday's result - the same lie the probation checklist was fixed to stop
telling.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.core.logging import get_logger
from app.core.recurrence import RECURRING, WorkingCalendar, next_due
from app.models.task import Task, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import LeaveRepository
from app.repositories.task import TaskRepository

logger = get_logger("app.task_recurrence")

SYSTEM_ACTOR = "system"

#: How far ahead to load approved leave. A daily task steps forward days,
#: not months; a sabbatical is better handled by pausing the series.
_LEAVE_WINDOW_DAYS = 30


@dataclass
class RecurrenceOutcome:
    """What one sweep did, for the worker's log line."""

    created: int = 0
    #: Series skipped because an instance is already open or due.
    already_current: int = 0
    #: Series stopped: the assignee left, or `recurs_until` has passed.
    stopped: list[str] = field(default_factory=list)
    #: Recurring tasks with no due date. They CANNOT generate - there is
    #: nothing to step from - and inventing a date would create hundreds of
    #: tasks out of a guess. Counted so the silence is visible instead of
    #: being the thing that made `cadence` meaningless for years.
    needs_a_due_date: int = 0

    @property
    def total(self) -> int:
        return self.created + len(self.stopped)


def _shifted(start: datetime | None, old_due: datetime | None, new_due: date) -> datetime | None:
    """`start` moved by the same distance the due date moved.

    None stays None - a task with no start date does not acquire one. If the
    old due date is missing there is no distance to preserve, so the start
    date is dropped rather than carried forward wrong.
    """
    if start is None or old_due is None:
        return None
    gap = old_due.date() - start.date()
    return datetime.combine(new_due - gap, time.min, tzinfo=UTC)


class TaskRecurrenceService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        leaves: LeaveRepository | None = None,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._audit = audit
        # Optional so a caller that only wants the pure scheduling - a test,
        # a dry run - need not wire leave in. When absent, leave simply is
        # not consulted; it never guesses that somebody is present.
        self._leaves = leaves

    async def run_due(
        self, today: date, calendar_: WorkingCalendar | None = None
    ) -> RecurrenceOutcome:
        """Create the next instance of every recurring task that needs one.

        `calendar_` is the org's open days - working weekdays from
        `AttendancePolicy` and closed dates from `holidays`. It applies to
        DAILY only: a daily task on a Sunday or on Diwali is noise, but
        moving a WEEKLY task off its day changes what it means.
        """
        outcome = RecurrenceOutcome()
        latest = await self._tasks.recurring_due(sorted(RECURRING, key=lambda c: c.value))
        # Defensive dedupe. Two instances of one series sharing a due date is
        # not supposed to happen, but the "newest per series" join would then
        # return both and the series would be considered twice - harmless
        # because of the idempotency check, and still worth not doing.
        # One query for every assignee's leave, not one per task.
        away = await self._days_off(latest, today)

        seen: set[uuid.UUID] = set()
        for task in latest:
            series = task.series_id or task.id
            if series in seen:
                continue
            seen.add(series)
            await self._consider(task, today, self._for_assignee(calendar_, away, task), outcome)

        # Reported, never guessed at. The fix is one date from a human.
        outcome.needs_a_due_date = len(
            await self._tasks.recurring_without_a_due_date(sorted(RECURRING, key=lambda c: c.value))
        )
        return outcome

    async def _days_off(self, tasks: Sequence[Task], today: date) -> dict[uuid.UUID, set[date]]:
        """Approved leave per assignee, over the window a task might move into.

        A month: a daily task only ever steps forward a few days, and a long
        sabbatical is better handled by pausing the series than by loading a
        year of leave every tick.
        """
        if self._leaves is None or not tasks:
            return {}
        return await self._leaves.days_off_for(
            [t.assignee_id for t in tasks], today, today + timedelta(days=_LEAVE_WINDOW_DAYS)
        )

    @staticmethod
    def _for_assignee(
        base: WorkingCalendar | None,
        away: dict[uuid.UUID, set[date]],
        task: Task,
    ) -> WorkingCalendar | None:
        """The org's calendar with this assignee's leave laid over it."""
        days = away.get(task.assignee_id)
        if base is None or not days:
            return base
        return base.minus(frozenset(days))

    async def _consider(
        self,
        task: Task,
        today: date,
        calendar_: WorkingCalendar | None,
        outcome: RecurrenceOutcome,
    ) -> None:
        series = task.series_id or task.id
        if task.due_date is None or task.cadence not in RECURRING:
            return

        # Idempotency, before anything else is computed. This is the single
        # check that makes an hourly sweep safe.
        if await self._tasks.series_has_due_on_or_after(series, today):
            outcome.already_current += 1
            return

        due = next_due(task.due_date.date(), task.cadence, today, calendar_)
        if due is None:
            # Absurdly stale, or a working week with no working days. Both are
            # data problems, and inventing a date would bury them.
            outcome.stopped.append(f"{task.id}:undatable")
            return
        if task.recurs_until is not None and due > task.recurs_until:
            task.recurrence_active = False
            outcome.stopped.append(f"{task.id}:ended")
            return
        if not await self._assignee_is_here(task):
            task.recurrence_active = False
            outcome.stopped.append(f"{task.id}:assignee_gone")
            return

        await self._create(task, series, due, outcome)

    async def _assignee_is_here(self, task: Task) -> bool:
        """A series whose assignee left stops rather than piling up work
        nobody holds.

        Paused, not deleted: HR reassigning the person's work can switch it
        back on, and the completed history stays either way.
        """
        person = await self._employees.get(task.assignee_id)
        return person is not None and person.is_active

    async def _create(
        self, task: Task, series: uuid.UUID, due: date, outcome: RecurrenceOutcome
    ) -> None:
        fresh = self._next_instance(task, series, due)
        self._tasks.add(fresh)
        await self._tasks.flush()
        await self._audit.append(
            actor=SYSTEM_ACTOR,
            action="task.recurred",
            target=f"task:{fresh.id}:series:{series}:{task.cadence.value}",
        )
        outcome.created += 1

    @staticmethod
    def _next_instance(task: Task, series: uuid.UUID, due: date) -> Task:
        """The next instance: the agreement, and nothing about the last one.

        Copied, because a human wrote them and they are what the work IS:
        title, description, assignee, project, priority, expected output,
        cadence, and the series' own end date and active flag.

        NOT copied, and each one matters:

        * `status`, `completion_pct` - it has not been started.
        * `final_outcome`, `review_notes`, `remarks` - Monday's task arriving
          pre-filled with Friday's result is a lie about work nobody did.
        * `escalated`, `escalation_level` - last week being late says nothing
          about this week, and carrying the level would suppress the warning.
        * `blocked_reason`, `completed_at` - state of a different instance.
        * `collaborators` - people added to unblock ONE late task should not
          inherit the series forever. They are re-added by escalation if it
          happens again.
        """
        return Task(
            title=task.title,
            description=task.description,
            assignee_id=task.assignee_id,
            assigned_by_id=task.assigned_by_id,
            project=task.project,
            project_id=task.project_id,
            project_link=task.project_link,
            priority=task.priority,
            expected_output=task.expected_output,
            cadence=task.cadence,
            status=TaskStatus.TODO,
            # SHIFTED by the same amount as the due date, not copied. A task
            # that starts two days before it is due should keep that gap;
            # copying the original start date unchanged made every instance's
            # start date one cadence step staler than the last, until a daily
            # task claimed to have been open for months.
            start_date=_shifted(task.start_date, task.due_date, due),
            # UTC, explicitly. A naive datetime is interpreted in the
            # SERVER's timezone, so on an IST box a task due the 12th was
            # stored as 18:30 on the 11th and read back as the 11th - a
            # whole day early, every time. SQLite stores naive values
            # happily, so only real Postgres showed it.
            due_date=datetime.combine(due, time.min, tzinfo=UTC),
            series_id=series,
            recurs_until=task.recurs_until,
            recurrence_active=True,
        )
