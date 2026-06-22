"""Org-settings data access — a single workspace-identity row (singleton)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_settings import OrgSettings


class OrgSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> OrgSettings | None:
        rows = await self._session.execute(
            select(OrgSettings).order_by(OrgSettings.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self) -> OrgSettings:
        org = OrgSettings()
        self._session.add(org)
        await self._session.flush()
        return org

    async def flush(self) -> None:
        await self._session.flush()
