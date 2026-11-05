"""The Room's wire shapes."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

MAX_BODY = 500


class CelebrationRead(BaseModel):
    """Somebody whose day it is today."""

    employee_id: uuid.UUID
    name: str
    kind: str  # "birthday" | "anniversary"
    #: Years of service, for an anniversary. None for a birthday - Avora does
    #: not publish anyone's age.
    years: int | None = None
    topic_key: str


class RoomMessageRead(ORMModel):
    id: uuid.UUID
    body: str
    author_id: uuid.UUID | None
    author_name: str | None = None
    topic_key: str
    pinned_at: datetime | None
    created_at: datetime


class RoomMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY)
    #: Which topic the note belongs to. Validated against the topics the Room is
    #: actually showing today - a key the server did not just offer is refused.
    topic_key: str = Field(min_length=1, max_length=128)


class RoomEventRead(BaseModel):
    """The next thing on the calendar, as the rail's torn-off ticket."""

    id: uuid.UUID
    title: str
    starts_at: datetime
    location: str | None
    all_day: bool
    topic_key: str
    going_count: int
    going_names: list[str] = Field(default_factory=list)
    #: None when the caller has not answered yet.
    my_rsvp: bool | None = None


class RsvpSet(BaseModel):
    attending: bool = True


class RoomRead(BaseModel):
    """Everything the rail draws, in one request."""

    today: date
    celebrations: list[CelebrationRead] = Field(default_factory=list)
    event: RoomEventRead | None = None
    messages: list[RoomMessageRead] = Field(default_factory=list)
    #: The keys a note may be posted against right now.
    open_topics: list[str] = Field(default_factory=list)
    general_topic: str
