"""Work-session data access — open-session lookups + per-day spans."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work_session import WorkSession


class WorkSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_open(self, employee_id: uuid.UUID) -> WorkSession | None:
        """The employee's currently-open session (not yet clocked out), if any."""
        rows = await self._session.execute(
            select(WorkSession)
            .where(
                WorkSession.employee_id == employee_id,
                WorkSession.clock_out_at.is_(None),
            )
            .order_by(WorkSession.clock_in_at.desc())
            .limit(1)
        )
        return rows.scalar_one_or_none()

    async def create(
        self, *, employee_id: uuid.UUID, clock_in_at: datetime, source: str
    ) -> WorkSession:
        ws = WorkSession(employee_id=employee_id, clock_in_at=clock_in_at, source=source)
        self._session.add(ws)
        await self._session.flush()
        return ws

    async def flush(self) -> None:
        await self._session.flush()

    async def day_spans(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, tuple[datetime, datetime | None]]:
        """Per-employee (first clock-in, last clock-out) within the day. The
        clock-out is None when any session that day is still open."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(
                WorkSession.employee_id,
                func.min(WorkSession.clock_in_at),
                func.max(WorkSession.clock_out_at),
                func.count().filter(WorkSession.clock_out_at.is_(None)),
            )
            .where(
                WorkSession.employee_id.in_(employee_ids),
                WorkSession.clock_in_at >= start,
                WorkSession.clock_in_at < end,
            )
            .group_by(WorkSession.employee_id)
        )
        spans: dict[uuid.UUID, tuple[datetime, datetime | None]] = {}
        for emp_id, first_in, last_out, open_count in rows.all():
            spans[emp_id] = (first_in, None if open_count else last_out)
        return spans
