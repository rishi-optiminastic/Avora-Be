"""The Room - the shared notice board on the dashboard.

One place where the whole company can see what today is about and say
something about it: a birthday, a work anniversary, the next event. Everything
else in Avora is work addressed to one person; this is the only surface that is
addressed to everybody.

Two tables:

* `room_messages` - the notes people leave. A note is attached to a **topic**
  (today's birthday, a specific event) or to nothing at all, so a wish for Fiza
  does not drift onto next week's board. Everyone may post and everyone may
  read: a board an employee cannot see is a board nobody can wish anyone on,
  which removes the point of having it.
* `event_rsvps` - one row per person per event.

`Event`'s own docstring says event answers live in `FormResponse` rather than a
table of their own, and that is right for a sign-up with questions. An RSVP has
no questions - it is one bit, set by one click, and attaching a form to every
social event so somebody can say "count me in" would be the heavier mechanism,
not the lighter one. Hence a table, kept deliberately small.

Retention is 90 days, enforced when messages are READ (see
`RoomRepository.recent`) rather than by a nightly delete. The scheduler fleet
has been pointed at the wrong database for weeks; a retention promise that
depends on a job nobody is watching is not a promise.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: How far back the board reads. Older notes stay on disk but stop being served.
RETENTION_DAYS = 90


class RoomTopicKind(StrEnum):
    """What a note is about."""

    CELEBRATION = "celebration"  # a birthday or work anniversary, on a given day
    EVENT = "event"  # a published event
    GENERAL = "general"  # the board itself


class RoomMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "room_messages"

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    body: Mapped[str] = mapped_column(Text)

    topic_kind: Mapped[RoomTopicKind] = mapped_column(default=RoomTopicKind.GENERAL)
    #: Stable within a topic and across days: "birthday:<employee id>:2026-11-05",
    #: "event:<event id>". Built by `app.services.room_topics`, never by a client -
    #: a client-chosen key would let anyone file a note under someone else's day.
    topic_key: Mapped[str] = mapped_column(String(128), default="general", index=True)

    #: Pinning is curation, so it is HR/admin only even though posting is not.
    pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    pinned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        # The board's only read pattern: newest first within a topic.
        Index("ix_room_messages_topic_created", "topic_key", "created_at"),
    )


class EventRsvp(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "event_rsvps"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    #: False is a real answer ("can't make it"), not an absent row.
    attending: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (
        # One answer per person per event; changing your mind updates this row.
        UniqueConstraint("event_id", "employee_id", name="uq_event_rsvp_person"),
    )
