"""Event schemas - what HR writes, and what the audience reads back."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.event import EventStatus
from app.schemas.audience import AudienceRead, AudienceSpec

_MAX_TITLE = 200
_MAX_DESCRIPTION = 10_000
_MAX_LOCATION = 300


class EventWrite(BaseModel):
    """Create or edit an event. HR and admins only (enforced in the service)."""

    title: str = Field(min_length=1, max_length=_MAX_TITLE)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION)
    location: str | None = Field(default=None, max_length=_MAX_LOCATION)
    starts_at: datetime
    ends_at: datetime | None = None
    all_day: bool = False
    audience: AudienceSpec = Field(default_factory=AudienceSpec)
    # The form that collects answers for this event (an RSVP, a feedback
    # round). Must be a PUBLISHED form; the service checks.
    form_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> EventWrite:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("The event cannot end before it starts.")
        return self


class EventRead(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    status: EventStatus
    audience: AudienceRead
    form_id: uuid.UUID | None
    form_title: str | None = None
    form_sent_at: datetime | None = None
    # Answers in, out of people asked. Only filled for HR/admin: the audience
    # does not get to see who has and has not replied.
    form_submitted: int | None = None
    form_pending: int | None = None
    # This caller's own assignment for the event form, so the page can offer
    # "Respond" instead of making them hunt through Forms.
    my_assignment_id: uuid.UUID | None = None
    created_by: uuid.UUID | None
    created_by_name: str | None = None
    published_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    # What the caller may do, from the same rules the write paths enforce.
    can_manage: bool = False
