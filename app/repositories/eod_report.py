"""End-of-Day report data access. Authorization (whose reports a caller may see)
is enforced by the service via the employee scope; this layer only builds queries.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eod_report import EodReport, EodStatus


class EodReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, report_id: uuid.UUID) -> EodReport | None:
        return await self._session.get(EodReport, report_id)

    async def get_for_day(self, employee_id: uuid.UUID, report_date: str) -> EodReport | None:
        report: EodReport | None = await self._session.scalar(
            select(EodReport).where(
                EodReport.employee_id == employee_id,
                EodReport.report_date == report_date,
            )
        )
        return report

    async def create(
        self,
        *,
        employee_id: uuid.UUID,
        report_date: str,
        status: EodStatus,
        summary: str = "",
        highlights: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> EodReport:
        report = EodReport(
            employee_id=employee_id,
            report_date=report_date,
            status=status,
            summary=summary,
            highlights=highlights or {},
            metrics=metrics or {},
            model=model,
            error=error,
        )
        self._session.add(report)
        await self._session.flush()
        return report

    async def list_for_employees(
        self, employee_ids: Sequence[uuid.UUID], report_date: str
    ) -> Sequence[EodReport]:
        if not employee_ids:
            return []
        rows = await self._session.execute(
            select(EodReport).where(
                EodReport.employee_id.in_(employee_ids),
                EodReport.report_date == report_date,
            )
        )
        return rows.scalars().all()

    async def list_for_employee_between(
        self, employee_id: uuid.UUID, start_date: str, end_date: str
    ) -> Sequence[EodReport]:
        """One employee's reports within [start_date, end_date] (inclusive, local
        YYYY-MM-DD). ISO dates sort lexicographically, so the string range is
        chronological. Newest first — this is the per-person history feed."""
        rows = await self._session.execute(
            select(EodReport)
            .where(
                EodReport.employee_id == employee_id,
                EodReport.report_date >= start_date,
                EodReport.report_date <= end_date,
            )
            .order_by(EodReport.report_date.desc())
        )
        return rows.scalars().all()

    async def list_for_employees_between(
        self, employee_ids: Sequence[uuid.UUID], start_date: str, end_date: str
    ) -> Sequence[EodReport]:
        """Every report for a set of employees within [start_date, end_date]
        (inclusive). Feeds the team cumulative digest; newest first."""
        if not employee_ids:
            return []
        rows = await self._session.execute(
            select(EodReport)
            .where(
                EodReport.employee_id.in_(employee_ids),
                EodReport.report_date >= start_date,
                EodReport.report_date <= end_date,
            )
            .order_by(EodReport.report_date.desc())
        )
        return rows.scalars().all()

    async def list_drafts_through(self, through_date: str) -> Sequence[EodReport]:
        """Drafts for `report_date` on or before `through_date` (local YYYY-MM-DD)
        still awaiting review — the auto-send set. ISO dates sort lexicographically,
        so the string compare is chronological."""
        rows = await self._session.execute(
            select(EodReport)
            .where(
                EodReport.status == EodStatus.DRAFT,
                EodReport.report_date <= through_date,
            )
            .order_by(EodReport.report_date)
        )
        return rows.scalars().all()

    async def flush(self) -> None:
        await self._session.flush()
