"""Out-of-office data access — requests, overlap checks, approved-day lookups."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.out_of_office import OutOfOffice, OutOfOfficePortion, OutOfOfficeStatus

_LIVE = (OutOfOfficeStatus.PENDING, OutOfOfficeStatus.APPROVED)


def expand_days(start_day: str, end_day: str) -> list[str]:
    """Every local date the entry spans, inclusive."""
    first, last = date.fromisoformat(start_day), date.fromisoformat(end_day)
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


class OutOfOfficeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        employee_id: uuid.UUID,
        start_day: str,
        end_day: str,
        portion: OutOfOfficePortion,
        location: str,
        reason: str,
    ) -> OutOfOffice:
        entry = OutOfOffice(
            employee_id=employee_id,
            start_day=start_day,
            end_day=end_day,
            portion=portion,
            location=location,
            reason=reason,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def get(self, entry_id: uuid.UUID) -> OutOfOffice | None:
        return await self._session.get(OutOfOffice, entry_id)

    async def overlapping(
        self, employee_id: uuid.UUID, start_day: str, end_day: str
    ) -> OutOfOffice | None:
        """A pending or approved entry already covering any of these dates.

        Two live entries on one day would each claim it, and on approval the day
        would be credited twice. Rejected and cancelled ones are ignored - those
        dates are free again.
        """
        rows = await self._session.execute(
            select(OutOfOffice).where(
                OutOfOffice.employee_id == employee_id,
                OutOfOffice.status.in_(_LIVE),
                OutOfOffice.start_day <= end_day,
                OutOfOffice.end_day >= start_day,
            )
        )
        return rows.scalars().first()

    async def list_for_employees(
        self,
        employee_ids: Sequence[uuid.UUID],
        *,
        month: str | None = None,
        status: OutOfOfficeStatus | None = None,
    ) -> Sequence[OutOfOffice]:
        if not employee_ids:
            return []
        stmt = select(OutOfOffice).where(OutOfOffice.employee_id.in_(employee_ids))
        if month is not None:
            # Any entry that touches the month, including one spanning its edge.
            stmt = stmt.where(
                OutOfOffice.start_day <= f"{month}-31", OutOfOffice.end_day >= f"{month}-01"
            )
        if status is not None:
            stmt = stmt.where(OutOfOffice.status == status)
        rows = await self._session.execute(stmt.order_by(OutOfOffice.start_day.desc()))
        return rows.scalars().all()

    async def approved_days(
        self, employee_ids: Sequence[uuid.UUID], start_day: str, end_day: str
    ) -> dict[tuple[uuid.UUID, str], OutOfOfficePortion]:
        """Approved out-of-office dates in the window, as (employee, day) -> portion.

        Expanded to one entry per date so attendance can look a day up directly
        rather than scanning ranges for every employee on every day it renders.
        """
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(OutOfOffice).where(
                OutOfOffice.employee_id.in_(employee_ids),
                OutOfOffice.status == OutOfOfficeStatus.APPROVED,
                OutOfOffice.start_day <= end_day,
                OutOfOffice.end_day >= start_day,
            )
        )
        out: dict[tuple[uuid.UUID, str], OutOfOfficePortion] = {}
        for entry in rows.scalars().all():
            for day in expand_days(entry.start_day, entry.end_day):
                if start_day <= day <= end_day:
                    out[(entry.employee_id, day)] = entry.portion
        return out

    async def flush(self) -> None:
        await self._session.flush()
