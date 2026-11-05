"""The Room's business rules.

The rules that carry weight:

* **Everyone reads and everyone posts.** A board an employee cannot see is a
  board nobody can wish anyone on, which removes the point of having it. This
  is the one surface in Avora with no scoping by reporting line.
* **Topics come from the server.** A note is filed against today's celebration
  or a published event, and the caller may only use a key the Room just
  offered. Otherwise anyone could post onto someone else's day by guessing a
  string, or revive last year's thread.
* **Your own note is yours to delete; pinning is HR and admin.** Posting is
  open, curation is not - a pinned note is the board speaking, not a person.
* **Ninety days, enforced on read.** `RoomRepository` filters every query by
  the window, so the retention promise holds whether or not the housekeeping
  delete has ever run. The scheduler fleet has been pointed at the wrong
  database for weeks; a promise that depends on an unwatched job is not one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.event import Event
from app.models.room import RoomMessage
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.event import EventRepository
from app.repositories.room import RoomRepository
from app.schemas.auth import CurrentUser
from app.schemas.room import (
    CelebrationRead,
    RoomEventRead,
    RoomMessageCreate,
    RoomMessageRead,
    RoomRead,
)
from app.services.celebration_service import CelebrationService
from app.services.room_topics import GENERAL_KEY, celebration_key, event_key, kind_of

logger = logging.getLogger(__name__)

#: Names shown on the event ticket before it collapses to a count.
_FACES = 5


class RoomService:
    def __init__(
        self,
        room: RoomRepository,
        events: EventRepository,
        employees: EmployeeRepository,
        celebrations: CelebrationService,
        audit: AuditRepository,
    ) -> None:
        self._room = room
        self._events = events
        self._employees = employees
        self._celebrations = celebrations
        self._audit = audit

    async def board(self, caller: CurrentUser, now: datetime) -> RoomRead:
        """Everything the rail draws, in one read."""
        today = now.date()
        celebrations = [
            CelebrationRead(
                employee_id=person.id,
                name=person.full_name,
                kind=kind,
                years=years,
                topic_key=celebration_key(person.id, today),
            )
            for person, kind, years in await self._celebrations.celebrations_on(today)
        ]
        event = await self._next_event(caller, now)

        topics = [c.topic_key for c in celebrations]
        if event is not None:
            topics.append(event.topic_key)
        topics.append(GENERAL_KEY)

        rows = await self._room.recent(topics, now=now)
        return RoomRead(
            today=today,
            celebrations=celebrations,
            event=event,
            messages=[_to_message(m, name) for m, name in rows],
            open_topics=topics,
            general_topic=GENERAL_KEY,
        )

    async def since(
        self, caller: CurrentUser, now: datetime, after: datetime | None
    ) -> list[RoomMessageRead]:
        """Just the new notes, for the polling hook."""
        board = await self.board(caller, now)
        if after is None:
            return board.messages
        rows = await self._room.recent(board.open_topics, now=now, since=after)
        return [_to_message(m, name) for m, name in rows]

    async def post(
        self, caller: CurrentUser, payload: RoomMessageCreate, now: datetime
    ) -> RoomMessageRead:
        board = await self.board(caller, now)
        # The only accepted keys are the ones this caller was just offered. A
        # client cannot invent a topic, and yesterday's keys stop working.
        if payload.topic_key not in board.open_topics:
            raise ValidationError("That is not something the Room is showing today.")
        body = payload.body.strip()
        if not body:
            raise ValidationError("Write something first.")
        message = await self._room.add(
            author_id=caller.employee_id,
            body=body,
            topic_kind=kind_of(payload.topic_key),
            topic_key=payload.topic_key,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="room.post",
            target=f"topic:{payload.topic_key}",
        )
        person = await self._employees.get(caller.employee_id)
        return _to_message(message, person.full_name if person else None)

    async def delete(self, caller: CurrentUser, message_id: uuid.UUID) -> None:
        """Your own note, or anyone's if you are HR/admin."""
        message = await self._room.get(message_id)
        if message is None:
            raise NotFoundError()
        if message.author_id != caller.employee_id and not caller.is_admin:
            raise AuthorizationError()
        await self._room.remove(message)
        await self._audit.append(
            actor=str(caller.employee_id), action="room.delete", target=f"message:{message_id}"
        )

    async def set_pin(
        self, caller: CurrentUser, message_id: uuid.UUID, *, pinned: bool, now: datetime
    ) -> RoomMessageRead:
        """Curation, so admin only - a pinned note reads as the board speaking."""
        if not caller.is_admin:
            raise AuthorizationError()
        message = await self._room.get(message_id)
        if message is None:
            raise NotFoundError()
        message.pinned_at = now if pinned else None
        message.pinned_by = caller.employee_id if pinned else None
        await self._room.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="room.pin" if pinned else "room.unpin",
            target=f"message:{message_id}",
        )
        person = await self._employees.get(message.author_id) if message.author_id else None
        return _to_message(message, person.full_name if person else None)

    async def rsvp(
        self, caller: CurrentUser, event_id: uuid.UUID, *, attending: bool
    ) -> RoomEventRead:
        event = await self._events.get(event_id)
        if event is None:
            raise NotFoundError()
        await self._room.set_rsvp(
            event_id=event_id, employee_id=caller.employee_id, attending=attending
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.rsvp",
            target=f"event:{event_id}:{'yes' if attending else 'no'}",
        )
        return await self._event_read(event, caller)

    # --- internals --------------------------------------------------------- #
    async def _next_event(self, caller: CurrentUser, now: datetime) -> RoomEventRead | None:
        upcoming = await self._events.upcoming_published(now)
        if not upcoming:
            return None
        return await self._event_read(upcoming[0], caller)

    async def _event_read(self, event: Event, caller: CurrentUser) -> RoomEventRead:
        rows = await self._room.rsvps_for(event.id)
        mine = await self._room.my_rsvp(event.id, caller.employee_id)
        names = [name for _, name in rows if name]
        return RoomEventRead(
            id=event.id,
            title=event.title,
            starts_at=event.starts_at,
            location=event.location,
            all_day=event.all_day,
            topic_key=event_key(event.id),
            going_count=len(rows),
            going_names=names[:_FACES],
            my_rsvp=None if mine is None else mine.attending,
        )


def _to_message(message: RoomMessage, author_name: str | None) -> RoomMessageRead:
    return RoomMessageRead(
        id=message.id,
        body=message.body,
        author_id=message.author_id,
        author_name=author_name,
        topic_key=message.topic_key,
        pinned_at=message.pinned_at,
        created_at=message.created_at,
    )
