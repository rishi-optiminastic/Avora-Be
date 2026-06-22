"""End-of-Day report data access. Authorization (whose reports a caller may see)
is enforced by the service via the employee scope; this layer only builds queries.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

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
        model: str | None = None,
        error: str | None = None,
    ) -> EodReport:
        report = EodReport(
            employee_id=employee_id,
            report_date=report_date,
            status=status,
            summary=summary,
            highlights=highlights or {},
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

    async def list_overdue_drafts(self, cutoff: datetime) -> Sequence[EodReport]:
        """Drafts created before `cutoff` and still awaiting review (auto-send)."""
        rows = await self._session.execute(
            select(EodReport).where(
                EodReport.status == EodStatus.DRAFT,
                EodReport.created_at < cutoff,
            )
        )
        return rows.scalars().all()

    async def flush(self) -> None:
        await self._session.flush()
