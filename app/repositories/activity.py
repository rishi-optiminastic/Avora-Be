"""Activity data access — insert raw samples, scoped reads."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivitySample


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
