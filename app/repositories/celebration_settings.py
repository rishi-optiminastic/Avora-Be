"""Celebration-settings data access — the singleton toggle row."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celebration_settings import CelebrationSettings


class CelebrationSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> CelebrationSettings | None:
        rows = await self._session.execute(select(CelebrationSettings).limit(1))
        return rows.scalar_one_or_none()

    async def create_default(self) -> CelebrationSettings:
        row = CelebrationSettings()
        self._session.add(row)
        await self._session.flush()
        return row

    async def flush(self) -> None:
        await self._session.flush()
