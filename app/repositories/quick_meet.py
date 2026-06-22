"""Quick Meet default-invitee data access. The ONLY place these queries are built.

Every method is owner-scoped — callers pass their own `owner_id` and never see
another employee's list (Golden rule #3). Queries are parameterized via the ORM.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quick_meet_default import QuickMeetDefault


class QuickMeetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_owner(self, owner_id: uuid.UUID) -> QuickMeetDefault | None:
        result = await self._session.execute(
            select(QuickMeetDefault).where(QuickMeetDefault.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def set_for_owner(self, owner_id: uuid.UUID, emails: list[str]) -> QuickMeetDefault:
        """Upsert the caller's default invitees and return the stored row."""
        row = await self.get_for_owner(owner_id)
        if row is None:
            row = QuickMeetDefault(owner_id=owner_id, invitee_emails=emails)
            self._session.add(row)
        else:
            row.invitee_emails = emails
        await self._session.flush()
        return row
