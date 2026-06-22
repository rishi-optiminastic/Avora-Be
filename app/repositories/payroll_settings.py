"""Payroll-settings data access — a single org-wide row (singleton)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll_settings import PayrollSettings


class PayrollSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> PayrollSettings | None:
        rows = await self._session.execute(
            select(PayrollSettings).order_by(PayrollSettings.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self) -> PayrollSettings:
        settings = PayrollSettings()
        self._session.add(settings)
        await self._session.flush()
        return settings

    async def flush(self) -> None:
        await self._session.flush()
