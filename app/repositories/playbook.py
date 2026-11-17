"""Playbook data access - the only place these queries are built."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.playbook import (
    Playbook,
    PlaybookRun,
    PlaybookRunStatus,
    PlaybookStatus,
    PlaybookStep,
    PlaybookTrigger,
)
from app.models.resignation import Resignation, ResignationStatus
from app.models.task import Task, TaskStatus


class PlaybookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, playbook: Playbook) -> None:
        self._session.add(playbook)

    def add_step(self, step: PlaybookStep) -> None:
        self._session.add(step)

    async def flush(self) -> None:
        await self._session.flush()

    async def get(self, playbook_id: uuid.UUID) -> Playbook | None:
        return await self._session.get(Playbook, playbook_id)

    async def list_all(self) -> Sequence[Playbook]:
        rows = await self._session.scalars(
            select(Playbook)
            .where(Playbook.status != PlaybookStatus.ARCHIVED)
            .order_by(Playbook.created_at.desc())
        )
        return list(rows)

    async def steps_of(self, playbook_id: uuid.UUID) -> Sequence[PlaybookStep]:
        rows = await self._session.scalars(
            select(PlaybookStep)
            .where(PlaybookStep.playbook_id == playbook_id)
            .order_by(PlaybookStep.position)
        )
        return list(rows)

    async def step_counts(self) -> dict[uuid.UUID, int]:
        """How many steps each playbook has, in ONE query.

        The library page needs counts, not step rows. Asking per playbook was
        1 + N queries AND loaded every step to call len() on it - measured at
        9 queries and 80 rows for 8 playbooks. A GROUP BY is flat in both.
        """
        rows = await self._session.execute(
            select(PlaybookStep.playbook_id, func.count()).group_by(PlaybookStep.playbook_id)
        )
        return dict(rows.all())  # type: ignore[arg-type]

    async def delete_steps_of(self, playbook_id: uuid.UUID) -> None:
        for step in await self.steps_of(playbook_id):
            await self._session.delete(step)

    async def published_with_trigger(
        self, triggers: Sequence[PlaybookTrigger]
    ) -> Sequence[Playbook]:
        """Every published playbook on one of these triggers.

        Plural, unlike `live_for_trigger`: finance and ops can each own a
        month-end checklist, so the monthly sweep iterates rather than
        picking one.
        """
        rows = await self._session.scalars(
            select(Playbook).where(
                Playbook.trigger.in_(triggers),
                Playbook.status == PlaybookStatus.PUBLISHED,
            )
        )
        return list(rows)

    async def live_for_trigger(self, trigger: PlaybookTrigger) -> Playbook | None:
        """The one PUBLISHED playbook for an automatic trigger.

        `uq_playbooks_live_trigger` guarantees at most one, so this cannot
        quietly pick between two.
        """
        live: Playbook | None = await self._session.scalar(
            select(Playbook).where(
                Playbook.trigger == trigger,
                Playbook.status == PlaybookStatus.PUBLISHED,
            )
        )
        return live


class PlaybookRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, run: PlaybookRun) -> None:
        self._session.add(run)

    def add_task(self, task: Task) -> None:
        """A task a run stamped out. Here rather than in TaskRepository so
        the run and its tasks are written through one unit of work - a run
        that reports 11 tasks must not be able to have written 9."""
        self._session.add(task)

    async def flush(self) -> None:
        await self._session.flush()

    async def get(self, run_id: uuid.UUID) -> PlaybookRun | None:
        return await self._session.get(PlaybookRun, run_id)

    async def already_ran(
        self, trigger: PlaybookTrigger, subject_id: uuid.UUID, period: str = ""
    ) -> bool:
        """Has this subject already had this trigger run FOR THIS PERIOD?

        THE idempotency check, and it mirrors `uq_playbook_runs_trigger_subject`
        exactly: the period is part of the key, so "" means once ever and
        "2026-11" means once in November. Cancelled runs do not count, so a
        cancelled run can be done again. Checked in the service as well as in
        the database so the sweep skips quietly instead of raising every tick.
        """
        found = await self._session.scalar(
            select(PlaybookRun.id).where(
                PlaybookRun.trigger == trigger,
                PlaybookRun.subject_id == subject_id,
                PlaybookRun.period_key == period,
                PlaybookRun.status != PlaybookRunStatus.CANCELLED,
            )
        )
        return found is not None

    async def for_subject(self, subject_id: uuid.UUID) -> Sequence[PlaybookRun]:
        rows = await self._session.scalars(
            select(PlaybookRun)
            .where(PlaybookRun.subject_id == subject_id)
            .order_by(PlaybookRun.created_at.desc())
        )
        return list(rows)

    async def recent(self, limit: int = 50) -> Sequence[PlaybookRun]:
        rows = await self._session.scalars(
            select(PlaybookRun).order_by(PlaybookRun.created_at.desc()).limit(limit)
        )
        return list(rows)

    async def untouched_tasks_of(self, run_id: uuid.UUID) -> Sequence[Task]:
        """Tasks from this run that nobody has started.

        Cancelling a run deletes only these. A task somebody has already
        worked on is theirs now, whatever created it - deleting it would throw
        away real work, which is the opposite of the point.
        """
        rows = await self._session.scalars(
            select(Task).where(
                Task.playbook_run_id == run_id,
                Task.status == TaskStatus.TODO,
                Task.completion_pct == 0,
            )
        )
        return list(rows)

    async def delete_task(self, task: Task) -> None:
        await self._session.delete(task)

    async def accepted_resignations(self, since: date) -> Sequence[Resignation]:
        """Accepted resignations with a last working day on or after `since`.

        Accepted only: an exit checklist raised off a resignation somebody is
        still deciding on would be visible to the leaver's manager before the
        decision is made.

        `since` does double duty, exactly as it does for joiners. It is the
        backfill guard - publishing an offboarding playbook must not raise an
        exit checklist for every resignation in company history - and it is
        what keeps this query bounded. Unbounded, an hourly tick would load
        every leaver the company has ever had, forever.
        """
        rows = await self._session.scalars(
            select(Resignation).where(
                Resignation.status == ResignationStatus.ACCEPTED,
                Resignation.last_working_day >= since,
            )
        )
        return list(rows)

    async def joiners_since(self, since: date) -> Sequence[Employee]:
        """Active employees with a hire date on or after `since`.

        The backfill guard in query form: without the date bound, publishing a
        new-joiner playbook would sweep up all 29 people already on the roster.
        """
        rows = await self._session.scalars(
            select(Employee).where(
                Employee.is_active.is_(True),
                Employee.hire_date.is_not(None),
                Employee.hire_date >= since,
            )
        )
        return list(rows)
