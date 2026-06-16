"""Agent ingest schemas — strict validation of a hostile payload (rule 5.4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ActivityIngest(BaseModel):
    """A single activity sample claimed by the agent.

    Every field is a claim to validate, not a fact. Impossible values are
    rejected; the agent's clock is recorded but never trusted for ordering.
    """

    model_config = {"extra": "forbid"}

    sequence: int = Field(ge=1, description="Monotonic per-device sequence number.")
    client_timestamp: datetime = Field(description="Agent-claimed time (untrusted).")
    active_window: str | None = Field(default=None, max_length=512)
    idle_seconds: int = Field(default=0, ge=0, le=86_400)
    # Active browser tab URL (optional). The server re-derives the domain from
    # this; a client-sent domain is never trusted (Golden rule #1).
    url: str | None = Field(default=None, max_length=2048)


class ActivityIngestResult(BaseModel):
    accepted: bool
    sequence: int
    flags: list[str]


class ActivitySampleRead(ORMModel):
    id: uuid.UUID
    device_id: uuid.UUID
    employee_id: uuid.UUID
    sequence: int
    client_timestamp: datetime
    received_at: datetime
    active_window: str | None
    idle_seconds: int
    url: str | None
    domain: str | None
    flags: list[str]
