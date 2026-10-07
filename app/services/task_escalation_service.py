"""Automatic overdue-task escalation - pure business rules, no I/O objects.

A task that slips gets louder on a schedule instead of waiting for a manager to
notice it:

    overdue >= 1 day   -> tier 1: warn the assignee (raises the dashboard pop-up)
    overdue >= 3 days  -> tier 2: add the reporting manager as a collaborator
    overdue >= 6 days  -> tier 3: add an admin as a collaborator

Adding a collaborator is the point, not a side effect: a collaborator gets the
task in their scope and can comment on it, so the people who need to unblock the
work can actually see and act on it rather than being told about it.

Each tier fires EXACTLY ONCE, tracked by `Task.escalation_level`. The sweep is
therefore idempotent: running it ten times a day, or re-running it after a crash
mid-way, never re-notifies or re-adds anyone. A task that is completed stops
escalating immediately, because every query filters out DONE.

Run by `worker/task_escalation_scheduler.py`. Thresholds are configurable so the
org can tune them without a deploy-time code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.task import Task
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.task import TaskRepository
from app.services.notification_service import NotificationService

logger = get_logger("app.task_escalation")

TIER_ASSIGNEE = 1
TIER_MANAGER = 2
TIER_ADMIN = 3


# `Notification.title` is String(160) while `Task.title` is String(256), so a long
# title made the INSERT fail. `create_isolated` swallows that in a SAVEPOINT and
# returns None, and the sweep then advanced the tier anyway - the assignee was
# never warned and the tier could never re-fire. Tests run on SQLite, which
# ignores VARCHAR widths, so nothing caught it.
_NOTIFICATION_TITLE_MAX = 160


def _notification_title(prefix: str, task_title: str) -> str:
    """`prefix` plus the task title, trimmed to fit the notification column."""
    room = _NOTIFICATION_TITLE_MAX - len(prefix)
    title = task_title if len(task_title) <= room else task_title[: room - 1].rstrip() + "…"
    return f"{prefix}{title}"


def _task_link(task: Task) -> str:
    return f"/dashboard/goals/tasks?task={task.id}"


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC value.

    Due dates are persisted as `DateTime(timezone=True)`, but a naive value comes
    back from backends that do not carry the offset (SQLite under test). Treat it
    as already-UTC rather than letting the machine's local timezone shift it -
    mirrors `LeaveService._utc_date`.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _overdue_days(task: Task, now: datetime) -> int:
    """Whole days past due. 0 when the task has no due date or is not yet late."""
    if task.due_date is None:
        return 0
    return max(0, (_as_utc(now) - _as_utc(task.due_date)).days)


@dataclass(frozen=True)
class EscalationOutcome:
    """What one sweep did - returned so the worker can log something meaningful
    and tests can assert on it without reading the database."""

    assignee_warned: int = 0
    managers_added: int = 0
    admins_added: int = 0

    @property
    def total(self) -> int:
        return self.assignee_warned + self.managers_added + self.admins_added


class TaskEscalationService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        notifications: NotificationService,
        audit: AuditRepository,
        *,
        warn_after_days: int = 1,
        manager_after_days: int = 3,
        admin_after_days: int = 6,
        batch_limit: int = 200,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._notifications = notifications
        self._audit = audit
        self._warn_after_days = warn_after_days
        self._manager_after_days = manager_after_days
        self._admin_after_days = admin_after_days
        self._batch_limit = batch_limit

    async def run_due(self, now: datetime) -> EscalationOutcome:
        """Escalate every task that has crossed a threshold since the last sweep.

        Tiers are applied in order, highest first within a task, so a task that
        has been overdue for a week lands on its final tier in one pass rather
        than taking three days of sweeps to catch up.

        Each tier is capped at `batch_limit` tasks per sweep. The first sweep
        after this is switched on walks the entire historical backlog, and an
        ESCALATION notification is rendered by the dashboard as a blocking
        full-screen modal - uncapped, one admin would have been handed a modal per
        stale task and nobody could use the app until they had clicked through
        every one. The remainder is simply picked up by the next tick.
        """
        warned = await self._warn_assignees(now)
        managers = await self._add_managers(now)
        admins = await self._add_admins(now)
        return EscalationOutcome(
            assignee_warned=warned, managers_added=managers, admins_added=admins
        )

    # ---- tier 1: the assignee ---------------------------------------------- #
    async def _warn_assignees(self, now: datetime) -> int:
        cutoff = now - timedelta(days=self._warn_after_days)
        count = 0
        overdue = await self._tasks.list_overdue_for_escalation(
            cutoff, below_level=TIER_ASSIGNEE, limit=self._batch_limit
        )
        for task in overdue:
            days = _overdue_days(task, now)
            await self._notifications.notify(
                recipient_id=task.assignee_id,
                kind=NotificationKind.ESCALATION,
                title=_notification_title("Overdue: ", task.title),
                body=f"{days} day{'s' if days != 1 else ''} past due. Update it or flag a blocker.",
                level=NotificationLevel.WARNING,
                link=_task_link(task),
                entity_type="task",
                entity_id=task.id,
            )
            await self._mark(task, TIER_ASSIGNEE, "overdue_warned")
            count += 1
        return count

    # ---- tier 2: the reporting manager ------------------------------------- #
    async def _add_managers(self, now: datetime) -> int:
        cutoff = now - timedelta(days=self._manager_after_days)
        overdue = await self._tasks.list_overdue_for_escalation(
            cutoff, below_level=TIER_MANAGER, limit=self._batch_limit
        )
        if not overdue:
            return 0
        # Two batched lookups instead of two queries PER TASK. The first sweep
        # after enabling this walks the whole historical backlog at once, which
        # is exactly when an N+1 would be felt.
        assignees = await self._employees.get_many([t.assignee_id for t in overdue])
        manager_ids = {e.manager_id for e in assignees.values() if e.manager_id is not None}
        managers = await self._employees.get_many(list(manager_ids))

        count = 0
        for task in overdue:
            assignee = assignees.get(task.assignee_id)
            manager = (
                managers.get(assignee.manager_id)
                if assignee is not None and assignee.manager_id is not None
                else None
            )
            # No manager on file, or the assignee IS the manager: there is nobody
            # to pull in at this tier. Still advance the level, or the task would
            # be re-examined on every sweep forever and never reach tier 3.
            if await self._pull_in(task, manager, assignee, tier=TIER_MANAGER, now=now):
                count += 1
            else:
                await self._mark(task, TIER_MANAGER, "overdue_manager_unavailable")
        return count

    # ---- tier 3: an admin --------------------------------------------------- #
    async def _add_admins(self, now: datetime) -> int:
        cutoff = now - timedelta(days=self._admin_after_days)
        overdue = await self._tasks.list_overdue_for_escalation(
            cutoff, below_level=TIER_ADMIN, limit=self._batch_limit
        )
        if not overdue:
            return 0
        admin = await self._pick_admin()
        assignees = await self._employees.get_many([t.assignee_id for t in overdue])
        count = 0
        for task in overdue:
            if await self._pull_in(
                task, admin, assignees.get(task.assignee_id), tier=TIER_ADMIN, now=now
            ):
                count += 1
            else:
                await self._mark(task, TIER_ADMIN, "overdue_admin_unavailable")
        return count

    async def _pick_admin(self) -> Employee | None:
        """One admin to carry the final escalation.

        Deliberately a single person rather than every admin: the point is to give
        the task an owner senior enough to unblock it, and adding the whole admin
        group as collaborators on every stale task would make the escalation
        meaningless noise. Ordered by name, so the same admin is chosen every
        sweep instead of rotating unpredictably.

        IT_ADMIN counts: the org treats it as a full admin everywhere
        (`CurrentUser._normalize_role`), and an org whose only admins hold that
        stored role would otherwise have its last escalation tier quietly do
        nothing at all.
        """
        admins = [
            *await self._employees.list_by_role(Role.ADMIN),
            *await self._employees.list_by_role(Role.IT_ADMIN),
        ]
        active = sorted((a for a in admins if a.is_active), key=lambda a: a.full_name)
        return active[0] if active else None

    # ---- shared ------------------------------------------------------------- #
    async def _pull_in(
        self,
        task: Task,
        person: Employee | None,
        assignee: Employee | None,
        *,
        tier: int,
        now: datetime,
    ) -> bool:
        """Add `person` as a collaborator and tell them why. False when there is
        nobody to add, so the caller can still advance the tier."""
        if person is None or not person.is_active or person.id == task.assignee_id:
            return False
        await self._tasks.add_collaborator(task.id, person.id)  # idempotent
        days = _overdue_days(task, now)
        who = assignee.full_name if assignee is not None else "someone on your team"
        await self._notifications.notify(
            recipient_id=person.id,
            kind=NotificationKind.ESCALATION,
            title=_notification_title("Escalated to you: ", task.title),
            body=f"{who}'s task is {days} days overdue. You have been added as a collaborator.",
            level=NotificationLevel.CRITICAL if tier == TIER_ADMIN else NotificationLevel.WARNING,
            link=_task_link(task),
            entity_type="task",
            entity_id=task.id,
        )
        await self._mark(
            task,
            tier,
            "overdue_manager_added" if tier == TIER_MANAGER else "overdue_admin_added",
            pulled_in=True,
        )
        return True

    async def _mark(self, task: Task, level: int, action: str, *, pulled_in: bool = False) -> None:
        """Record that this tier has fired.

        `escalation_level` always advances - that is what stops a tier being
        re-examined on every sweep forever, and it must advance even when the
        tier found nobody to pull in.

        `escalated` - the flag the board badges "Escalated - manager/admin" - is
        a different claim: that somebody was actually brought onto the task. It
        is set only when that happened, and never for tier 1, which is a nudge to
        the person already holding the task. Setting it on an empty tier labelled
        tasks as escalated to a manager who was never added.
        """
        task.escalation_level = level
        if pulled_in and level >= TIER_MANAGER:
            task.escalated = True
        await self._tasks.flush()
        await self._audit.append(
            actor="system",
            action=f"task.{action}",
            target=f"task:{task.id}:level={level}",
        )
