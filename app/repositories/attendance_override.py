"""Attendance override data access — the only place these queries are built.

HR/Admin-managed (authorized in the service), so not row-scoped to a caller;
keyed by (employee, day). `for_range` is the bulk read the attendance computation
uses to force a day's status.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance_override import AttendanceOverride, AttendanceOverrideStatus
from app.schemas.attendance_override import AttendanceOverrideCreate


class AttendanceOverrideRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, payload: AttendanceOverrideCreate, *, created_by: uuid.UUID | None
    ) -> AttendanceOverride:
        """One override per (employee, day) — re-setting the same day replaces it."""
        existing = (
            await self._session.execute(
                select(AttendanceOverride).where(
                    AttendanceOverride.employee_id == payload.employee_id,
                    AttendanceOverride.day == payload.day,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = payload.status
            existing.note = payload.note
            existing.created_by = created_by
            await self._session.flush()
            return existing
        row = AttendanceOverride(
            employee_id=payload.employee_id,
            day=payload.day,
            status=payload.status,
            note=payload.note,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, override_id: uuid.UUID) -> AttendanceOverride | None:
        return await self._session.get(AttendanceOverride, override_id)

    async def delete(self, override_id: uuid.UUID) -> bool:
        row = await self._session.get(AttendanceOverride, override_id)
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def list_for_period(self, period_month: str) -> Sequence[AttendanceOverride]:
        """Overrides whose day falls in the YYYY-MM period (ISO dates sort as text)."""
        rows = await self._session.execute(
            select(AttendanceOverride)
            .where(AttendanceOverride.day.like(f"{period_month}-%"))
            .order_by(AttendanceOverride.day.desc())
        )
        return rows.scalars().all()

    async def for_range(
        self, employee_ids: Sequence[uuid.UUID], start_day: str, end_day: str
    ) -> dict[tuple[uuid.UUID, str], AttendanceOverrideStatus]:
        """{(employee_id, day): status} for the attendance computation. Org-wide;
        the caller (AttendanceService) is already scoped to its employee set."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(AttendanceOverride).where(
                AttendanceOverride.employee_id.in_(employee_ids),
                AttendanceOverride.day >= start_day,
                AttendanceOverride.day <= end_day,
            )
        )
        return {(r.employee_id, r.day): r.status for r in rows.scalars().all()}
