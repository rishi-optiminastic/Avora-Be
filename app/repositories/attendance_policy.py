"""Attendance-policy data access — a single org-wide row (singleton)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance_policy import AttendancePolicy


class AttendancePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> AttendancePolicy | None:
        rows = await self._session.execute(
            select(AttendancePolicy).order_by(AttendancePolicy.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self) -> AttendancePolicy:
        policy = AttendancePolicy()
        self._session.add(policy)
        await self._session.flush()
        return policy

    async def flush(self) -> None:
        await self._session.flush()
