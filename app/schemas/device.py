"""Device request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class DeviceCreate(BaseModel):
    """Admin/IT enrolls a device for an employee."""

    employee_id: uuid.UUID
    label: str = Field(min_length=1, max_length=256)


class DeviceCreated(BaseModel):
    """Returned on enrollment — carries the raw token, shown exactly once."""

    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    token: str
    created_at: datetime


class DeviceRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    last_sequence: int
    is_revoked: bool
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
