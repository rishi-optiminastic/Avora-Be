"""Announcement data access. Visibility (everyone reads, HR/Admin write) is
enforced by the service; this layer only builds queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.announcement import Announcement, AnnouncementLevel


class AnnouncementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        message: str,
        level: AnnouncementLevel,
        expires_at: datetime | None,
        created_by: uuid.UUID,
    ) -> Announcement:
        row = Announcement(
            message=message, level=level, expires_at=expires_at, created_by=created_by
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, announcement_id: uuid.UUID) -> Announcement | None:
        return await self._session.get(Announcement, announcement_id)

    async def list_active(self) -> Sequence[Announcement]:
        """Active, not-yet-expired announcements, newest first."""
        now = datetime.now(UTC)
        rows = await self._session.execute(
            select(Announcement)
            .where(
                Announcement.active.is_(True),
                or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
            )
            .order_by(Announcement.created_at.desc())
        )
        return rows.scalars().all()

    async def deactivate(self, announcement: Announcement) -> None:
        """Soft-retire — keep the row for history, drop it from the bar."""
        announcement.active = False
        await self._session.flush()
