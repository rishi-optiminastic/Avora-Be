"""Device data access — credential lookup and replay-safe sequence updates."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device


class DeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, device_id: uuid.UUID) -> Device | None:
        return await self._session.get(Device, device_id)

    async def get_active_by_token_hash(self, token_hash: str) -> Device | None:
        result = await self._session.execute(
            select(Device).where(
                Device.token_hash == token_hash,
                Device.is_revoked.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def advance_sequence(self, device: Device, sequence: int) -> None:
        """Move the high-water mark forward. Caller has already verified
        `sequence > device.last_sequence`."""
        device.last_sequence = sequence
        await self._session.flush()
