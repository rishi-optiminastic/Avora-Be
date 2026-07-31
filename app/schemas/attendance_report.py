"""Attendance range + monthly-report schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.monitoring import AttendanceStatus


class AttendanceDayRow(BaseModel):
    """One employee on one local day (for the date-range / per-employee views)."""

    employee_id: uuid.UUID
    day: str  # YYYY-MM-DD (local office date)
    status: AttendanceStatus
    login_at: datetime | None
    logout_at: datetime | None
    worked_minutes: int
    late_login: bool
    regularizable: bool
    regularized: bool
    clock_in_source: str | None = None
    clock_out_source: str | None = None


class AttendanceMonthSummary(BaseModel):
    """An employee's month: day counts + total hours (for the monthly report)."""

    employee_id: uuid.UUID
    full_days: int
    half_days: int
    late_days: int
    absent_days: int
    leave_days: int
    regularized_days: int
    worked_minutes: int
