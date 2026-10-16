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

A backlog never floods anyone. When a task is found past several tiers at once
(the first sweep after switching this on, or after the worker was down), only
the highest due tier's effect is applied - the manager and/or admin are added in
one step - and the task raises at most ONE pop-up per sweep; everyone else it
touches gets a quiet inbox note. On top of that each person gets at most
`popups_per_person` pop-ups per sweep, and the rest collapse into a single
"N more overdue tasks" summary.

Run by `worker/task_escalation_scheduler.py`. Thresholds are configurable so the
org can tune them without a deploy-time code change.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.models.employee import Employee
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

# How many escalation pop-ups (each a blocking modal on the dashboard) one person
# may receive from a single sweep. The rest collapse into one summary pop-up.
DEFAULT_POPUPS_PER_PERSON = 3

_BOARD_LINK = "/dashboard/goals/tasks"


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
    # Collapsed "N more overdue tasks" pop-ups sent to people over their cap.
    summaries_sent: int = 0

    @property
    def total(self) -> int:
        return self.assignee_warned + self.managers_added + self.admins_added


@dataclass(frozen=True)
class _Notice:
    """One message the sweep wants a person to see about one task."""

    recipient_id: uuid.UUID
    task: Task
    title: str
    body: str
    level: NotificationLevel
    # The single person this task raises a pop-up for this sweep. Everyone else
    # gets the same message as a quiet inbox note (`quiet_kind`).
    popup: bool
    quiet_kind: NotificationKind
    # True when the task was escalated TO this person (vs. their own task) -
    # only changes the wording of a collapsed summary.
    escalated_to_them: bool


class _PopupLedger:
    """Caps the pop-ups each person receives in one sweep."""

    def __init__(self, cap: int) -> None:
        self._cap = max(1, cap)
        self._sent: Counter[uuid.UUID] = Counter()
        self._own_overflow: Counter[uuid.UUID] = Counter()
        self._escalated_overflow: Counter[uuid.UUID] = Counter()

    def claim(self, recipient_id: uuid.UUID, *, escalated_to_them: bool) -> bool:
        """True when this person still has pop-up budget; otherwise the notice is
        counted toward their summary instead."""
        if self._sent[recipient_id] < self._cap:
            self._sent[recipient_id] += 1
            return True
        overflow = self._escalated_overflow if escalated_to_them else self._own_overflow
        overflow[recipient_id] += 1
        return False

    def overflow(self) -> list[tuple[uuid.UUID, int, int]]:
        """(recipient, own tasks collapsed, escalated tasks collapsed)."""
        people = set(self._own_overflow) | set(self._escalated_overflow)
        return [
            (pid, self._own_overflow[pid], self._escalated_overflow[pid])
            for pid in sorted(people, key=str)
        ]


class _AdminPool:
    """Spreads the admin tier across every admin, least-loaded first.

    "Load" is how many open tasks the admin already collaborates on, charged as
    the sweep hands out tasks - so a backlog is dealt out evenly instead of the
    first admin by name inheriting all of it.
    """

    def __init__(self, admins: Sequence[Employee], loads: dict[uuid.UUID, int]) -> None:
        self._admins = [a for a in admins if a.is_active]
        self._load = {a.id: loads.get(a.id, 0) for a in self._admins}

    def pick(self, exclude: set[uuid.UUID]) -> Employee | None:
        candidates = [a for a in self._admins if a.id not in exclude]
        if not candidates:
            return None
        chosen = min(candidates, key=lambda a: (self._load[a.id], a.full_name, str(a.id)))
        self._load[chosen.id] += 1
        return chosen


@dataclass
class _Sweep:
    """Everything one sweep looked up in batch, plus its running tallies."""

    now: datetime
    assignees: dict[uuid.UUID, Employee]
    managers: dict[uuid.UUID, Employee]
    admins: _AdminPool
    ledger: _PopupLedger
    tally: Counter[str] = field(default_factory=Counter)

    def manager_of(self, assignee: Employee | None) -> Employee | None:
        if assignee is None or assignee.manager_id is None:
            return None
        return self.managers.get(assignee.manager_id)


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
        popups_per_person: int = DEFAULT_POPUPS_PER_PERSON,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._notifications = notifications
        self._audit = audit
        self._tiers = (
            (TIER_ADMIN, admin_after_days),
            (TIER_MANAGER, manager_after_days),
            (TIER_ASSIGNEE, warn_after_days),
        )
        self._batch_limit = batch_limit
        self._popups_per_person = popups_per_person

    async def run_due(self, now: datetime) -> EscalationOutcome:
        """Escalate every task that has crossed a threshold since the last sweep.

        Each task is taken straight to its highest due tier in one pass, so a
        task overdue for a week lands on its final tier without three days of
        sweeps - but it raises one pop-up, not one per tier it skipped.

        Each tier's query is capped at `batch_limit` tasks per sweep, oldest
        first; the remainder is picked up by the next tick.
        """
        targets = await self._due_targets(now)
        if not targets:
            return EscalationOutcome()
        sweep = await self._prepare(targets, now)
        for task, tier in targets:
            await self._escalate(task, tier, sweep)
        summaries = await self._send_summaries(sweep.ledger)
        return EscalationOutcome(
            assignee_warned=sweep.tally["warned"],
            managers_added=sweep.tally["manager"],
            admins_added=sweep.tally["admin"],
            summaries_sent=summaries,
        )

    async def _due_targets(self, now: datetime) -> list[tuple[Task, int]]:
        """Every task with a tier due, paired with its HIGHEST due tier.

        One query per tier (each with its own cutoff), highest first, so a task
        that qualifies for several keeps only the top one. Returned oldest due
        date first: those are the ones that get individual pop-ups when a
        person's cap is reached.
        """
        by_id: dict[uuid.UUID, tuple[Task, int]] = {}
        for tier, after_days in self._tiers:
            overdue = await self._tasks.list_overdue_for_escalation(
                now - timedelta(days=after_days), below_level=tier, limit=self._batch_limit
            )
            for task in overdue:
                by_id.setdefault(task.id, (task, tier))
        return sorted(by_id.values(), key=lambda pair: _due_sort_key(pair[0]))

    async def _prepare(self, targets: list[tuple[Task, int]], now: datetime) -> _Sweep:
        """Batched lookups for the whole sweep - never a query per task. The first
        sweep after enabling walks the whole backlog, exactly when an N+1 bites."""
        assignees = await self._employees.get_many([t.assignee_id for t, _ in targets])
        manager_ids = {e.manager_id for e in assignees.values() if e.manager_id is not None}
        managers = await self._employees.get_many(list(manager_ids))
        admins: Sequence[Employee] = []
        loads: dict[uuid.UUID, int] = {}
        if any(tier >= TIER_ADMIN for _, tier in targets):
            admins = await self._employees.list_admins()
            loads = await self._tasks.open_collaboration_counts([a.id for a in admins])
        return _Sweep(
            now=now,
            assignees=assignees,
            managers=managers,
            admins=_AdminPool(admins, loads),
            ledger=_PopupLedger(self._popups_per_person),
        )

    # ---- one task ----------------------------------------------------------- #
    async def _escalate(self, task: Task, tier: int, sweep: _Sweep) -> None:
        """Apply `tier` to `task` once: pull people in, then notify them."""
        previous = task.escalation_level
        added, actions = await self._pull_in_people(task, tier, sweep)
        warn_assignee = previous < TIER_ASSIGNEE
        if warn_assignee:
            actions.append("overdue_warned")
            sweep.tally["warned"] += 1

        # One pop-up per task: the most senior person just brought in, or the
        # assignee when nobody was. Everyone else hears quietly via the inbox.
        notices = [
            self._escalated_notice(task, person, sweep, tier=added_by, popup=i == len(added) - 1)
            for i, (person, added_by) in enumerate(added)
        ]
        if warn_assignee:
            notices.append(self._assignee_notice(task, sweep.now, popup=not added))
        for notice in notices:
            await self._deliver(notice, sweep.ledger)
        await self._mark(task, tier, actions, pulled_in=bool(added))

    async def _pull_in_people(
        self, task: Task, tier: int, sweep: _Sweep
    ) -> tuple[list[tuple[Employee, int]], list[str]]:
        """Add the manager and/or an admin as collaborators, as `tier` requires.

        Returns who was added with the tier that added them (in tier order) and
        the audit actions. An empty tier is still recorded, so the task is not
        re-examined forever.
        """
        added: list[tuple[Employee, int]] = []
        actions: list[str] = []
        if tier >= TIER_MANAGER and task.escalation_level < TIER_MANAGER:
            manager = sweep.manager_of(sweep.assignees.get(task.assignee_id))
            if await self._add(task, manager) and manager is not None:
                added.append((manager, TIER_MANAGER))
                sweep.tally["manager"] += 1
                actions.append("overdue_manager_added")
            else:
                actions.append("overdue_manager_unavailable")
        if tier >= TIER_ADMIN:
            exclude = {task.assignee_id, *task.collaborator_ids, *(p.id for p, _ in added)}
            admin = sweep.admins.pick(exclude)
            if await self._add(task, admin) and admin is not None:
                added.append((admin, TIER_ADMIN))
                sweep.tally["admin"] += 1
                actions.append("overdue_admin_added")
            else:
                actions.append("overdue_admin_unavailable")
        return added, actions

    async def _add(self, task: Task, person: Employee | None) -> bool:
        """Add `person` as a collaborator. False when there is nobody to add."""
        if person is None or not person.is_active or person.id == task.assignee_id:
            return False
        await self._tasks.add_collaborator(task.id, person.id)  # idempotent
        return True

    # ---- notifications ------------------------------------------------------ #
    def _escalated_notice(
        self, task: Task, person: Employee, sweep: _Sweep, *, tier: int, popup: bool
    ) -> _Notice:
        assignee = sweep.assignees.get(task.assignee_id)
        who = assignee.full_name if assignee is not None else "someone on your team"
        days = _overdue_days(task, sweep.now)
        return _Notice(
            recipient_id=person.id,
            task=task,
            title=_notification_title("Escalated to you: ", task.title),
            body=f"{who}'s task is {days} days overdue. You have been added as a collaborator.",
            level=NotificationLevel.CRITICAL if tier == TIER_ADMIN else NotificationLevel.WARNING,
            popup=popup,
            quiet_kind=NotificationKind.TASK_ASSIGNED,
            escalated_to_them=True,
        )

    def _assignee_notice(self, task: Task, now: datetime, *, popup: bool) -> _Notice:
        days = _overdue_days(task, now)
        return _Notice(
            recipient_id=task.assignee_id,
            task=task,
            title=_notification_title("Overdue: ", task.title),
            body=f"{days} day{'s' if days != 1 else ''} past due. Update it or flag a blocker.",
            level=NotificationLevel.WARNING,
            popup=popup,
            quiet_kind=NotificationKind.SYSTEM,
            escalated_to_them=False,
        )

    async def _deliver(self, notice: _Notice, ledger: _PopupLedger) -> None:
        """Send a notice as a pop-up (ESCALATION) if it is this task's one pop-up
        and the person has budget left, as a quiet inbox note if it never was a
        pop-up, or not at all when it is collapsed into the person's summary."""
        if notice.popup:
            if not ledger.claim(notice.recipient_id, escalated_to_them=notice.escalated_to_them):
                return
            kind = NotificationKind.ESCALATION
        else:
            kind = notice.quiet_kind
        await self._notifications.notify(
            recipient_id=notice.recipient_id,
            kind=kind,
            title=notice.title,
            body=notice.body,
            level=notice.level,
            link=_task_link(notice.task),
            entity_type="task",
            entity_id=notice.task.id,
        )

    async def _send_summaries(self, ledger: _PopupLedger) -> int:
        """One pop-up per person over their cap, standing in for the rest."""
        sent = 0
        for recipient_id, own, escalated in ledger.overflow():
            await self._notifications.notify(
                recipient_id=recipient_id,
                kind=NotificationKind.ESCALATION,
                title=_summary_title(own, escalated),
                body="Open your task board to work through them, oldest first.",
                level=NotificationLevel.WARNING,
                link=_BOARD_LINK,
            )
            sent += 1
        return sent

    async def _mark(self, task: Task, level: int, actions: list[str], *, pulled_in: bool) -> None:
        """Record that `level` has been reached.

        `escalation_level` always advances - that is what stops a tier being
        re-examined on every sweep forever, and it must advance even when the
        tier found nobody to pull in.

        `escalated` - the flag the board badges "Escalated" - is a different
        claim: that somebody was actually brought onto the task. It is set only
        when that happened, and never for tier 1, which is a nudge to the person
        already holding the task. Which badge ("manager" or "admin") is derived
        from who is actually on the task (`Task.escalated_to`), never from the
        level, so an admin tier that found no admin cannot claim one.
        """
        task.escalation_level = level
        if pulled_in:
            task.escalated = True
        await self._tasks.flush()
        for action in actions:
            await self._audit.append(
                actor="system",
                action=f"task.{action}",
                target=f"task:{task.id}:level={level}",
            )


def _due_sort_key(task: Task) -> datetime:
    """Oldest due date first; the query guarantees a due date, this only types it."""
    return _as_utc(task.due_date) if task.due_date is not None else datetime.max.replace(tzinfo=UTC)


def _summary_title(own: int, escalated: int) -> str:
    total = own + escalated
    noun = "task" if total == 1 else "tasks"
    if own and escalated:
        return f"{total} more overdue {noun} need your attention"
    if escalated:
        return f"{total} more overdue {noun} escalated to you"
    return f"{total} more of your {noun} {'is' if total == 1 else 'are'} overdue"
