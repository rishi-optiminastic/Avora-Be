"""Festival data access — the only place festival queries are built."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.festival import Festival
from app.schemas.celebration import FestivalCreate


class FestivalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> Sequence[Festival]:
        rows = await self._session.execute(select(Festival).order_by(Festival.on_date.asc()))
        return rows.scalars().all()

    async def list_active_on(self, day: date) -> Sequence[Festival]:
        rows = await self._session.execute(
            select(Festival).where(Festival.on_date == day, Festival.is_active.is_(True))
        )
        return rows.scalars().all()

    async def create(self, payload: FestivalCreate) -> Festival:
        festival = Festival(
            name=payload.name.strip(),
            on_date=payload.on_date,
            message=payload.message.strip(),
            is_active=payload.is_active,
        )
        self._session.add(festival)
        await self._session.flush()
        return festival

    async def get(self, festival_id: uuid.UUID) -> Festival | None:
        return await self._session.get(Festival, festival_id)

    async def delete(self, festival: Festival) -> None:
        await self._session.delete(festival)
        await self._session.flush()
