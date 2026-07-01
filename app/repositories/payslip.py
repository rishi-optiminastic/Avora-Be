"""Payslip data access — the only place released-payslip queries are built.

Like compensation, no row-scope clause lives here: a payslip is fetched for one
employee (or one month) and the *service* authorizes the caller (self or
HR/Admin) before calling in. Released snapshots are immutable history; `upsert`
exists only so HR can re-finalize a month (e.g. after fixing attendance).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payslip import Payslip, PayslipStatus


class PayslipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, employee_id: uuid.UUID, period_month: str) -> Payslip | None:
        record: Payslip | None = await self._session.scalar(
            select(Payslip).where(
                Payslip.employee_id == employee_id,
                Payslip.period_month == period_month,
            )
        )
        return record

    async def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[Payslip]:
        """An employee's released payslips, newest month first."""
        rows = await self._session.execute(
            select(Payslip)
            .where(Payslip.employee_id == employee_id)
            .order_by(Payslip.period_month.desc())
        )
        return rows.scalars().all()

    async def list_for_month(self, period_month: str) -> Sequence[Payslip]:
        rows = await self._session.execute(
            select(Payslip).where(Payslip.period_month == period_month)
        )
        return rows.scalars().all()

    async def upsert(
        self,
        *,
        employee_id: uuid.UUID,
        period_month: str,
        employee_name: str,
        department: str | None,
        currency: str,
        monthly_ctc_minor: int,
        gross_minor: int,
        net_minor: int,
        breakdown: dict[str, int],
        working_days: int,
        present_days: float,
        paid_leave_days: float,
        payable_days: float,
        finalized_by: uuid.UUID | None,
    ) -> Payslip:
        """Create or refresh the (employee, month) snapshot and (re)release it."""
        record = await self.get(employee_id, period_month)
        if record is None:
            record = Payslip(employee_id=employee_id, period_month=period_month)
            self._session.add(record)
        record.employee_name = employee_name
        record.department = department
        record.currency = currency
        record.monthly_ctc_minor = monthly_ctc_minor
        record.gross_minor = gross_minor
        record.net_minor = net_minor
        record.breakdown = breakdown
        record.working_days = working_days
        record.present_days = present_days
        record.paid_leave_days = paid_leave_days
        record.payable_days = payable_days
        record.status = PayslipStatus.RELEASED
        record.released_at = datetime.now(UTC)
        record.finalized_by = finalized_by
        await self._session.flush()
        return record

    async def mark_emailed(self, record: Payslip) -> None:
        record.emailed_at = datetime.now(UTC)
        await self._session.flush()
