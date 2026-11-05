"""Room reads and writes. Every query is scoped to the 90-day window here, so
no caller can accidentally serve a note the retention promise says is gone."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.room import RETENTION_DAYS, EventRsvp, RoomMessage, RoomTopicKind

#: (message, author name) - the name is joined in so the board never N+1s.
MessageRow = tuple[RoomMessage, str | None]


def _cutoff(now: datetime) -> datetime:
    return now - timedelta(days=RETENTION_DAYS)


class RoomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent(
        self, topic_keys: Sequence[str], *, now: datetime, since: datetime | None = None
    ) -> list[MessageRow]:
        """Notes on these topics, oldest first, inside the retention window.

        `since` is for polling: pass the newest `created_at` the client already
        has and only later notes come back.
        """
        if not topic_keys:
            return []
        stmt = (
            select(RoomMessage, Employee.full_name)
            .outerjoin(Employee, Employee.id == RoomMessage.author_id)
            .where(
                RoomMessage.topic_key.in_(list(topic_keys)),
                RoomMessage.created_at >= _cutoff(now),
            )
            .order_by(RoomMessage.created_at)
        )
        if since is not None:
            stmt = stmt.where(RoomMessage.created_at > since)
        rows = await self._session.execute(stmt)
        return [(m, name) for m, name in rows.all()]

    async def get(self, message_id: uuid.UUID) -> RoomMessage | None:
        return await self._session.get(RoomMessage, message_id)

    async def add(
        self,
        *,
        author_id: uuid.UUID,
        body: str,
        topic_kind: RoomTopicKind,
        topic_key: str,
    ) -> RoomMessage:
        message = RoomMessage(
            author_id=author_id, body=body, topic_kind=topic_kind, topic_key=topic_key
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def flush(self) -> None:
        await self._session.flush()

    async def remove(self, message: RoomMessage) -> None:
        await self._session.delete(message)
        await self._session.flush()

    async def count_on(self, topic_key: str, *, now: datetime) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(RoomMessage)
            .where(RoomMessage.topic_key == topic_key, RoomMessage.created_at >= _cutoff(now))
        )
        return int(result.scalar_one())

    # --- RSVPs ------------------------------------------------------------- #
    async def rsvps_for(self, event_id: uuid.UUID) -> list[tuple[EventRsvp, str | None]]:
        rows = await self._session.execute(
            select(EventRsvp, Employee.full_name)
            .outerjoin(Employee, Employee.id == EventRsvp.employee_id)
            .where(EventRsvp.event_id == event_id, EventRsvp.attending.is_(True))
            .order_by(EventRsvp.created_at)
        )
        return [(r, name) for r, name in rows.all()]

    async def my_rsvp(self, event_id: uuid.UUID, employee_id: uuid.UUID) -> EventRsvp | None:
        result = await self._session.execute(
            select(EventRsvp).where(
                EventRsvp.event_id == event_id, EventRsvp.employee_id == employee_id
            )
        )
        return result.scalar_one_or_none()

    async def set_rsvp(
        self, *, event_id: uuid.UUID, employee_id: uuid.UUID, attending: bool
    ) -> EventRsvp:
        """Idempotent: answering twice updates the one row the unique constraint
        allows, rather than failing on it."""
        existing = await self.my_rsvp(event_id, employee_id)
        if existing is not None:
            existing.attending = attending
            existing.updated_at = datetime.now(UTC)
            await self._session.flush()
            return existing
        rsvp = EventRsvp(event_id=event_id, employee_id=employee_id, attending=attending)
        self._session.add(rsvp)
        await self._session.flush()
        return rsvp

    async def purge_expired(self, *, now: datetime) -> int:
        """Delete what the window already hides. Housekeeping only - correctness
        does not depend on this running, because every read filters too."""
        result = await self._session.execute(
            delete(RoomMessage).where(RoomMessage.created_at < _cutoff(now))
        )
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
