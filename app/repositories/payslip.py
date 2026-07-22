"""Payslip data access — the only place released-payslip queries are built.

Like compensation, no row-scope clause lives here: a payslip is fetched for one
employee (or one month) and the *service* authorizes the caller (self or
HR/Admin) before calling in. Released snapshots are immutable history; `upsert`
exists only so HR can re-finalize a month (e.g. after fixing attendance).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payslip import Payslip, PayslipStatus


@dataclass(frozen=True)
class PayslipSnapshot:
    """Everything frozen into one released (employee, month) payslip row.

    A DTO so `upsert` stays a single argument rather than ~18 keyword params;
    the service builds it once from a `PayrollLineRead`.
    """

    employee_id: uuid.UUID
    period_month: str
    employee_name: str
    department: str | None
    job_title: str | None
    location: str | None
    hire_date: date | None
    currency: str
    monthly_ctc_minor: int
    gross_minor: int
    net_minor: int
    breakdown: dict[str, int]
    prorated_breakdown: dict[str, int]
    total_days: int
    working_days: int
    present_days: float
    paid_leave_days: float
    payable_days: float
    finalized_by: uuid.UUID | None


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

    async def upsert(self, *, snapshot: PayslipSnapshot) -> Payslip:
        """Create or refresh the (employee, month) snapshot and (re)release it."""
        record = await self.get(snapshot.employee_id, snapshot.period_month)
        if record is None:
            record = Payslip(employee_id=snapshot.employee_id, period_month=snapshot.period_month)
            self._session.add(record)
        record.employee_name = snapshot.employee_name
        record.department = snapshot.department
        record.job_title = snapshot.job_title
        record.location = snapshot.location
        record.hire_date = snapshot.hire_date
        record.currency = snapshot.currency
        record.monthly_ctc_minor = snapshot.monthly_ctc_minor
        record.gross_minor = snapshot.gross_minor
        record.net_minor = snapshot.net_minor
        record.breakdown = snapshot.breakdown
        record.prorated_breakdown = snapshot.prorated_breakdown
        record.total_days = snapshot.total_days
        record.working_days = snapshot.working_days
        record.present_days = snapshot.present_days
        record.paid_leave_days = snapshot.paid_leave_days
        record.payable_days = snapshot.payable_days
        record.status = PayslipStatus.RELEASED
        record.released_at = datetime.now(UTC)
        record.finalized_by = snapshot.finalized_by
        await self._session.flush()
        return record

    async def mark_emailed(self, record: Payslip) -> None:
        record.emailed_at = datetime.now(UTC)
        await self._session.flush()
