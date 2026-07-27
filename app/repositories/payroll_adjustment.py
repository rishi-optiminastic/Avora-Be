"""Payroll adjustment data access — the only place these queries are built.

Adjustments are an HR/Admin management concern (authorized in the service), so
reads are not row-scoped to a caller; they are keyed by (employee, period_month).
`for_month` is the bulk read the payroll estimate/register uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll_adjustment import PayrollAdjustment
from app.schemas.payroll_adjustment import PayrollAdjustmentCreate


class PayrollAdjustmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, payload: PayrollAdjustmentCreate, *, created_by: uuid.UUID | None
    ) -> PayrollAdjustment:
        row = PayrollAdjustment(
            employee_id=payload.employee_id,
            period_month=payload.period_month,
            kind=payload.kind,
            label=payload.label,
            amount_minor=payload.amount_minor,
            target=payload.target,
            note=payload.note,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, adjustment_id: uuid.UUID) -> PayrollAdjustment | None:
        return await self._session.get(PayrollAdjustment, adjustment_id)

    async def delete(self, adjustment_id: uuid.UUID) -> bool:
        row = await self._session.get(PayrollAdjustment, adjustment_id)
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def list_for_period(self, period_month: str) -> Sequence[PayrollAdjustment]:
        rows = await self._session.execute(
            select(PayrollAdjustment)
            .where(PayrollAdjustment.period_month == period_month)
            .order_by(PayrollAdjustment.created_at.desc())
        )
        return rows.scalars().all()

    async def for_month(
        self, employee_ids: Sequence[uuid.UUID], period_month: str
    ) -> dict[uuid.UUID, list[PayrollAdjustment]]:
        """All adjustments for a payroll month, grouped by employee. Payroll (the
        already-authorized caller) reads org-wide here."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.employee_id.in_(employee_ids),
                PayrollAdjustment.period_month == period_month,
            )
        )
        out: dict[uuid.UUID, list[PayrollAdjustment]] = {}
        for row in rows.scalars().all():
            out.setdefault(row.employee_id, []).append(row)
        return out
