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
    # How much of a working day an approved out-of-office entry credits here:
    # 1.0 for a full day away, and for a half day 0.5 plus whatever the office
    # part of the day earned, capped at one. Carried on the row because the
    # monthly summary is built from these and cannot re-derive it.
    out_of_office_credit: float = 0.0
    # Where they were, for the board. None unless this day is out of office.
    out_of_office_location: str | None = None


class AttendanceMonthSummary(BaseModel):
    """An employee's month: day counts + total hours (for the monthly report)."""

    employee_id: uuid.UUID
    # Carried on the row because the roster includes leavers, whom the live
    # employee directory no longer lists: without them the UI can't name them.
    employee_name: str | None = None
    department: str | None = None
    full_days: int
    half_days: int
    late_days: int
    absent_days: int
    leave_days: int
    # Working days credited while away from the office. A float because a half
    # day counts as half - see `AttendanceDayRow.out_of_office_credit`.
    out_of_office_days: float = 0.0
    regularized_days: int
    worked_minutes: int
    # The typical clock-in / clock-out for the month, as local minutes past
    # midnight, averaged over the days that actually had one. None - not 0 -
    # when there were none all month: 0 renders as 00:00 and would read as a
    # midnight arrival rather than "never came in".
    avg_check_in_minutes: int | None = None
    avg_check_out_minutes: int | None = None
