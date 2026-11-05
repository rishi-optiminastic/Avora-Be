"""What a Room note is attached to, as a key the server alone decides.

A topic key is built here from facts the server already holds - whose birthday
it is today, which event is next - and never accepted from a client. A
client-chosen key would let anyone file a note under someone else's day, or
resurrect last year's birthday thread by guessing its string.

Keys carry the date for celebrations so next year's birthday starts a fresh
board rather than appending to the one from twelve months ago.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.models.room import RoomTopicKind

GENERAL_KEY = "general"


def celebration_key(employee_id: uuid.UUID, on: date) -> str:
    return f"celebration:{employee_id}:{on.isoformat()}"


def event_key(event_id: uuid.UUID) -> str:
    return f"event:{event_id}"


def kind_of(key: str) -> RoomTopicKind:
    """The kind a key belongs to - derived, so the two can never disagree."""
    if key.startswith("celebration:"):
        return RoomTopicKind.CELEBRATION
    if key.startswith("event:"):
        return RoomTopicKind.EVENT
    return RoomTopicKind.GENERAL
