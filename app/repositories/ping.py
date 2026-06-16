"""Ping data access — create, fetch an employee's undelivered pings, mark sent."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ping import Ping


class PingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, target_employee_id: uuid.UUID, issued_by_id: uuid.UUID, message: str | None
    ) -> Ping:
        ping = Ping(
            target_employee_id=target_employee_id,
            issued_by_id=issued_by_id,
            message=message,
        )
        self._session.add(ping)
        await self._session.flush()
        return ping

    async def pending_for_employee(self, employee_id: uuid.UUID) -> Sequence[Ping]:
        rows = await self._session.execute(
            select(Ping)
            .where(Ping.target_employee_id == employee_id, Ping.delivered_at.is_(None))
            .order_by(Ping.created_at.asc())
        )
        return rows.scalars().all()

    async def mark_delivered(self, ping_ids: Sequence[uuid.UUID], when: datetime) -> None:
        if not ping_ids:
            return
        await self._session.execute(
            update(Ping).where(Ping.id.in_(ping_ids)).values(delivered_at=when)
        )
        await self._session.flush()
