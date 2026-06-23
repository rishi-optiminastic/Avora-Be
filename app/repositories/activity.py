"""Activity data access — insert raw samples, scoped reads + rollups."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
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
        url: str | None,
        domain: str | None,
        page_title: str | None = None,
        browser: str | None = None,
        flags: list[str],
    ) -> ActivitySample:
        sample = ActivitySample(
            device_id=device_id,
            employee_id=employee_id,
            sequence=sequence,
            client_timestamp=client_timestamp,
            active_window=active_window,
            idle_seconds=idle_seconds,
            url=url,
            domain=domain,
            page_title=page_title,
            browser=browser,
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

    async def browsing_by_day(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, dict[date, list[tuple[str, int]]]]:
        """Per-employee, per-UTC-day (domain, count) over a range — for the
        productivity trend. The UTC cast keeps day buckets stable regardless of
        the DB session timezone."""
        if not employee_ids:
            return {}
        day = func.date(ActivitySample.received_at)
        stmt = (
            select(
                ActivitySample.employee_id, day.label("day"), ActivitySample.domain, func.count()
            )
            .where(
                ActivitySample.employee_id.in_(employee_ids),
                ActivitySample.received_at >= start,
                ActivitySample.received_at < end,
                ActivitySample.domain.is_not(None),
            )
            .group_by(ActivitySample.employee_id, day, ActivitySample.domain)
        )
        rows = await self._session.execute(stmt)
        result: dict[uuid.UUID, dict[date, list[tuple[str, int]]]] = {}
        for emp_id, d, domain, count in rows.all():
            # Postgres returns a date; SQLite returns a 'YYYY-MM-DD' string.
            day_key = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
            result.setdefault(emp_id, {}).setdefault(day_key, []).append((domain, int(count)))
        return result

    async def browsing_by_domain(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, list[tuple[str, int]]]:
        """Per-employee (domain, sample_count) over a day, for non-null domains.
        At one sample/minute the count approximates minutes on that domain."""
        if not employee_ids:
            return {}
        stmt = (
            select(
                ActivitySample.employee_id,
                ActivitySample.domain,
                func.count(),
            )
            .where(
                ActivitySample.employee_id.in_(employee_ids),
                ActivitySample.received_at >= start,
                ActivitySample.received_at < end,
                ActivitySample.domain.is_not(None),
            )
            .group_by(ActivitySample.employee_id, ActivitySample.domain)
        )
        rows = await self._session.execute(stmt)
        result: dict[uuid.UUID, list[tuple[str, int]]] = {}
        for emp_id, domain, count in rows.all():
            result.setdefault(emp_id, []).append((domain, int(count)))
        return result

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

    async def purge_before(self, cutoff: datetime) -> int:
        """Delete activity samples received before `cutoff` (retention). Returns
        the number of rows removed. Uses `ix_activity_received_at` for the scan."""
        result = await self._session.execute(
            delete(ActivitySample).where(ActivitySample.received_at < cutoff)
        )
        return cast("CursorResult[Any]", result).rowcount or 0
