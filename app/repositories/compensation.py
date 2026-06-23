"""Compensation data access — the only place compensation queries are built.

No row-scope clause lives here: compensation is fetched one employee at a time
and the *service* authorizes the caller (HR/Admin or self) before calling in.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compensation import Compensation
from app.schemas.compensation import CompensationWrite


class CompensationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_employee(self, employee_id: uuid.UUID) -> Compensation | None:
        record: Compensation | None = await self._session.scalar(
            select(Compensation).where(Compensation.employee_id == employee_id)
        )
        return record

    async def get_for_employees(
        self, employee_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Compensation]:
        """Batch lookup keyed by employee_id — avoids the per-employee N+1 in the
        org-wide payroll estimate."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(Compensation).where(Compensation.employee_id.in_(employee_ids))
        )
        return {c.employee_id: c for c in rows.scalars().all()}

    async def upsert(
        self, employee_id: uuid.UUID, data: CompensationWrite, *, updated_by: uuid.UUID
    ) -> Compensation:
        record = await self.get_for_employee(employee_id)
        if record is None:
            record = Compensation(employee_id=employee_id)
            self._session.add(record)
        record.amount_minor = data.amount_minor
        record.bonus_minor = data.bonus_minor
        record.currency = data.currency.upper()
        record.period = data.period
        record.effective_date = data.effective_date
        record.note = data.note
        record.updated_by = updated_by
        await self._session.flush()
        return record
