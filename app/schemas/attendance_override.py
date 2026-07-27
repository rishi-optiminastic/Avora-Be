"""Attendance override request/response schemas (Golden rule #5)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.attendance_override import AttendanceOverrideStatus
from app.schemas.common import ORMModel

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AttendanceOverrideCreate(BaseModel):
    """HR/Admin force one employee's attendance for a local office date."""

    employee_id: uuid.UUID
    day: str  # YYYY-MM-DD
    status: AttendanceOverrideStatus
    note: str | None = Field(default=None, max_length=500)

    @field_validator("day")
    @classmethod
    def _valid_day(cls, value: str) -> str:
        if not _DAY_RE.match(value):
            raise ValueError("day must be YYYY-MM-DD.")
        return value


class AttendanceOverrideRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    day: str
    status: AttendanceOverrideStatus
    note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
