"""Recognition request/response schemas (Golden rule #5).

Names and avatars are carried on the read models because the feed is read by the
whole org, including people who cannot open the employee directory - without them
a recognition would render as two UUIDs congratulating each other.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.recognition import RecognitionBadge, RecognitionCadence


class BadgeRead(BaseModel):
    """One badge in the published list, for the award form and the feed legend."""

    badge: RecognitionBadge
    emoji: str
    label: str
    cadence: RecognitionCadence
    when_to_use: str


def _meaningful(value: str) -> str:
    """Trim, and refuse what is left if it is nothing.

    `min_length=1` alone lets three spaces through, which then strip to an empty
    note or an empty comment sitting in the thread.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("must not be blank")
    return trimmed


class RecognitionCreate(BaseModel):
    employee_id: uuid.UUID
    badge: RecognitionBadge
    # Required: a badge with no reason is hollow, and the note is the part
    # colleagues actually respond to.
    note: str = Field(min_length=1, max_length=1000)

    _clean_note = field_validator("note")(_meaningful)


class RecognitionCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=1000)

    _clean_body = field_validator("body")(_meaningful)


class RecognitionCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recognition_id: uuid.UUID
    author_id: uuid.UUID | None
    author_name: str | None = None
    author_avatar_url: str | None = None
    body: str
    created_at: datetime
    # Whether THIS caller may delete this comment, decided by the server so the
    # UI never offers a button the API would refuse.
    can_delete: bool = False


class RecognitionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None
    employee_avatar_url: str | None = None
    awarded_by_id: uuid.UUID | None
    awarded_by_name: str | None = None
    badge: RecognitionBadge
    emoji: str = ""
    badge_label: str = ""
    note: str
    created_at: datetime
    comments: list[RecognitionCommentRead] = []
    can_delete: bool = False
