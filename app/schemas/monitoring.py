"""Derived attendance + live-activity response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    LATE = "late"
    ABSENT = "absent"


class AttendanceRead(BaseModel):
    """One employee's derived attendance for a day."""

    employee_id: uuid.UUID
    status: AttendanceStatus
    login_at: datetime | None
    logout_at: datetime | None
    worked_minutes: int
    idle_minutes: int
    active_minutes: int
    productivity_pct: int


class ActivityNowRead(BaseModel):
    """One employee's current live state (from their latest recent sample)."""

    employee_id: uuid.UUID
    online: bool
    idle: bool
    active_window: str | None
    idle_seconds: int
    last_seen_at: datetime | None
