"""Onboarding-config data access — a single org-wide row (singleton)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.onboarding_config import DEFAULT_STEPS, OnboardingConfig


class OnboardingConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> OnboardingConfig | None:
        rows = await self._session.execute(
            select(OnboardingConfig).order_by(OnboardingConfig.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self) -> OnboardingConfig:
        config = OnboardingConfig(steps=list(DEFAULT_STEPS))
        self._session.add(config)
        await self._session.flush()
        return config

    async def flush(self) -> None:
        await self._session.flush()
