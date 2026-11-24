"""Founder Meetings settings data access - a single row per workspace.

Same shape as `OrgSettingsRepository`: the oldest row wins, and a workspace
that has never been configured gets a default one on first read rather than a
null that every caller has to branch on.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting_settings import MeetingSettings


class MeetingSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> MeetingSettings | None:
        rows = await self._session.execute(
            select(MeetingSettings).order_by(MeetingSettings.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def get_or_create(self) -> MeetingSettings:
        """The workspace's row, created with defaults if it has none.

        Defaults deliberately leave `ceo_employee_id` null: the feature should
        read as "not set up yet" until an admin names a person, not quietly pick
        one.
        """
        existing = await self.get()
        if existing is not None:
            return existing
        row = MeetingSettings()
        self._session.add(row)
        await self._session.flush()
        return row

    async def flush(self) -> None:
        await self._session.flush()
