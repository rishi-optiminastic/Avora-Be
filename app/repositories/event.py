"""Event data access. Scope is decided in the service; this builds queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventStatus
from app.schemas.audience import AudienceKind


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: uuid.UUID) -> Event | None:
        return await self._session.get(Event, event_id)

    def add(self, event: Event) -> None:
        self._session.add(event)

    async def delete(self, event: Event) -> None:
        await self._session.delete(event)

    async def flush(self) -> None:
        await self._session.flush()

    async def list_all(self, *, upcoming_only: bool = False) -> Sequence[Event]:
        """Everything, for HR and admins - drafts and cancellations included.

        Soonest first for upcoming, most recent first for the full list: an
        organiser opening this wants the next thing at the top, and a historian
        wants the last thing.
        """
        stmt = select(Event)
        if upcoming_only:
            stmt = stmt.where(Event.status == EventStatus.PUBLISHED)
        rows = await self._session.scalars(stmt.order_by(Event.starts_at.desc()))
        return list(rows.all())

    async def list_visible_to(
        self, employee_id: uuid.UUID, department: str | None
    ) -> Sequence[Event]:
        """Published and cancelled events this person is in the audience for.

        Drafts are excluded: an unfinished event is not an announcement.
        Cancelled ones are kept, because "it was called off" is information
        somebody who was invited needs.

        The department match is done in Python after a narrower SQL filter
        rather than in the JSON column, because `audience_departments` is a
        JSON array whose containment operators differ between Postgres and the
        SQLite used by the tests - and this list is small.
        """
        stmt = (
            select(Event)
            .where(Event.status.in_((EventStatus.PUBLISHED, EventStatus.CANCELLED)))
            .order_by(Event.starts_at.desc())
        )
        rows = (await self._session.scalars(stmt)).all()
        mine = (department or "").strip().casefold()
        return [
            event
            for event in rows
            if _includes(event, employee_id, mine) or event.created_by == employee_id
        ]

    async def upcoming_published(self, after: datetime) -> Sequence[Event]:
        rows = await self._session.scalars(
            select(Event)
            .where(Event.status == EventStatus.PUBLISHED, Event.starts_at >= after)
            .order_by(Event.starts_at)
        )
        return list(rows.all())


def _includes(event: Event, employee_id: uuid.UUID, department_folded: str) -> bool:
    """Whether this stored audience reaches this person.

    Mirrors `AudienceResolver`: trimmed, case-insensitive department names,
    because `employees.department` is free text typed by HR.
    """
    if event.audience_kind is AudienceKind.EVERYONE:
        return True
    if event.audience_kind is AudienceKind.DEPARTMENTS:
        return department_folded in {
            d.strip().casefold() for d in event.audience_departments if d.strip()
        }
    return str(employee_id) in set(event.audience_employee_ids)
