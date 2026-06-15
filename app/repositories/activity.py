"""Activity data access — insert raw samples, scoped reads + rollups."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivitySample


@dataclass
class DailyAgg:
    """Per-employee rollup of one day's raw samples."""

    login_at: datetime
    logout_at: datetime
    sample_count: int
    idle_seconds: int


class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_sample(
        self,
        *,
        device_id: uuid.UUID,
        employee_id: uuid.UUID,
        sequence: int,
        client_timestamp: datetime,
        active_window: str | None,
        idle_seconds: int,
        flags: list[str],
    ) -> ActivitySample:
        sample = ActivitySample(
            device_id=device_id,
            employee_id=employee_id,
            sequence=sequence,
            client_timestamp=client_timestamp,
            active_window=active_window,
            idle_seconds=idle_seconds,
            flags=flags,
        )
        self._session.add(sample)
        await self._session.flush()
        return sample

    async def daily_aggregates(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, DailyAgg]:
        """min/max receive time, sample count and total idle per employee, for a
        day window. Server-stamped `received_at` is the only time we trust."""
        if not employee_ids:
            return {}
        stmt = (
            select(
                ActivitySample.employee_id,
                func.min(ActivitySample.received_at),
                func.max(ActivitySample.received_at),
                func.count(),
                func.coalesce(func.sum(ActivitySample.idle_seconds), 0),
            )
            .where(
                ActivitySample.employee_id.in_(employee_ids),
                ActivitySample.received_at >= start,
                ActivitySample.received_at < end,
            )
            .group_by(ActivitySample.employee_id)
        )
        rows = await self._session.execute(stmt)
        result: dict[uuid.UUID, DailyAgg] = {}
        for emp_id, login, logout, count, idle in rows.all():
            result[emp_id] = DailyAgg(
                login_at=login, logout_at=logout, sample_count=int(count), idle_seconds=int(idle)
            )
        return result

    async def latest_since(
        self, employee_ids: Sequence[uuid.UUID], since: datetime
    ) -> dict[uuid.UUID, ActivitySample]:
        """The most recent sample per employee since `since` (for the live view)."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(ActivitySample)
            .where(
                ActivitySample.employee_id.in_(employee_ids),
                ActivitySample.received_at >= since,
            )
            .order_by(ActivitySample.received_at.desc())
        )
        latest: dict[uuid.UUID, ActivitySample] = {}
        for sample in rows.scalars():
            latest.setdefault(sample.employee_id, sample)
        return latest

    async def samples_for_employee(
        self, employee_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[ActivitySample]:
        rows = await self._session.execute(
            select(ActivitySample)
            .where(
                ActivitySample.employee_id == employee_id,
                ActivitySample.received_at >= start,
                ActivitySample.received_at < end,
            )
            .order_by(ActivitySample.received_at.asc())
        )
        return rows.scalars().all()
