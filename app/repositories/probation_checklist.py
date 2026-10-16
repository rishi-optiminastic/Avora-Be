"""Checklist rows. Queries only — every scope decision lives in the service."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus

# Prefix of the note on a step Avora settled itself (no person as the actor).
SYSTEM_NOTE = "Done in Avora: "


class ProbationChecklistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[ProbationChecklistItem]:
        rows = await self._session.scalars(
            select(ProbationChecklistItem)
            .where(ProbationChecklistItem.employee_id == employee_id)
            .order_by(ProbationChecklistItem.step_no)
        )
        return list(rows)

    async def counts_for(
        self, employee_ids: Sequence[uuid.UUID], step_nos: Sequence[int]
    ) -> dict[uuid.UUID, int]:
        """Settled (done or skipped) step count per employee, for the roster.

        One query for the whole list — a per-employee count would be an N+1 on a
        page whose whole job is showing everyone at once.

        Restricted to the CURRENT template. Rows survive a step being retired, so
        an unfiltered count would show "12 of 11 steps" on the roster while the
        stepper beside it showed 11 - two different answers on one screen.
        """
        if not employee_ids or not step_nos:
            return {}
        rows = await self._session.execute(
            select(ProbationChecklistItem.employee_id, func.count())
            .where(
                ProbationChecklistItem.employee_id.in_(employee_ids),
                ProbationChecklistItem.step_no.in_(step_nos),
                ProbationChecklistItem.status != ProbationStepStatus.PENDING,
            )
            .group_by(ProbationChecklistItem.employee_id)
        )
        return {emp_id: int(count) for emp_id, count in rows.all()}

    async def get(self, employee_id: uuid.UUID, step_no: int) -> ProbationChecklistItem | None:
        row: ProbationChecklistItem | None = await self._session.scalar(
            select(ProbationChecklistItem).where(
                ProbationChecklistItem.employee_id == employee_id,
                ProbationChecklistItem.step_no == step_no,
            )
        )
        return row

    def add(self, item: ProbationChecklistItem) -> None:
        self._session.add(item)

    async def flush(self) -> None:
        await self._session.flush()

    async def mark_done_by_system(
        self, employee_id: uuid.UUID, notes: Mapping[int, str], at: datetime
    ) -> None:
        """Settle steps as Avora (no actor), leaving a person's own ticks alone.

        A step Avora settled earlier is refreshed, so a changed outcome does not
        leave the previous outcome's note behind.
        """
        for step_no, note in notes.items():
            row = await self.get(employee_id, step_no)
            if row is None:
                row = ProbationChecklistItem(employee_id=employee_id, step_no=step_no)
                self.add(row)
            elif row.status is not ProbationStepStatus.PENDING and not _settled_by_system(row):
                continue  # a person already settled it: theirs stands
            row.status = ProbationStepStatus.DONE
            row.note = SYSTEM_NOTE + note
            row.actor_id = None
            row.acted_at = at
        await self.flush()


def _settled_by_system(row: ProbationChecklistItem) -> bool:
    return row.actor_id is None and (row.note or "").startswith(SYSTEM_NOTE)
