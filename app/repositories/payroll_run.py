"""Payroll-run data access — send history + the auto-send idempotency guard."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll_run import PayrollRun, PayrollRunSource


class PayrollRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_month(self, period_month: str) -> PayrollRun | None:
        run: PayrollRun | None = await self._session.scalar(
            select(PayrollRun).where(PayrollRun.period_month == period_month)
        )
        return run

    async def upsert(
        self,
        *,
        period_month: str,
        currency: str,
        total_net_minor: int,
        employee_count: int,
        recipients: str,
        source: PayrollRunSource,
        triggered_by: uuid.UUID | None,
    ) -> PayrollRun:
        run = await self.get_for_month(period_month)
        if run is None:
            run = PayrollRun(period_month=period_month)
            self._session.add(run)
        run.currency = currency
        run.total_net_minor = total_net_minor
        run.employee_count = employee_count
        run.recipients = recipients
        run.source = source
        run.triggered_by = triggered_by
        await self._session.flush()
        return run

    async def list_recent(self, *, limit: int = 12) -> Sequence[PayrollRun]:
        rows = await self._session.execute(
            select(PayrollRun).order_by(PayrollRun.period_month.desc()).limit(limit)
        )
        return rows.scalars().all()
