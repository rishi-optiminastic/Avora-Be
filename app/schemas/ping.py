"""Ping request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.ping import PingKind
from app.schemas.common import ORMModel


class PingCreate(BaseModel):
    """A manager/admin pings an employee with an optional short message."""

    employee_id: uuid.UUID
    message: str | None = Field(default=None, max_length=280)


class CaptureRequest(BaseModel):
    """A manager/admin asks an employee's agent to take a screenshot now."""

    employee_id: uuid.UUID


class PingRead(ORMModel):
    """What the agent receives from its inbox."""

    id: uuid.UUID
    kind: PingKind
    message: str | None
    created_at: datetime
