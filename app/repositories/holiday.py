"""Holiday data access — org-wide calendar, readable by everyone."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.holiday import Holiday
from app.schemas.holiday import HolidayCreate


class HolidayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: HolidayCreate, *, actor_id: uuid.UUID) -> Holiday:
        holiday = Holiday(
            name=payload.name,
            date=payload.date,
            holiday_type=payload.holiday_type,
            created_by=actor_id,
            updated_by=actor_id,
        )
        self._session.add(holiday)
        await self._session.flush()
        return holiday

    async def get(self, holiday_id: uuid.UUID) -> Holiday | None:
        return await self._session.get(Holiday, holiday_id)

    async def list_all(self, *, offset: int, limit: int) -> tuple[Sequence[Holiday], int]:
        base = select(Holiday)
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self._session.execute(
            base.order_by(Holiday.date.asc()).offset(offset).limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, holiday: Holiday) -> None:
        await self._session.delete(holiday)
        await self._session.flush()
