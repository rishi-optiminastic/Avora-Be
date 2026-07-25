"""Attendance classification — pure, testable rules over the org policy.

Given when someone clocked in (UTC), how long they worked, whether the day was
regularized, and the policy, decide FULL_DAY / HALF_DAY / LATE / ABSENT plus the
payroll flags. Kept free of DB/ORM so it can be unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.schemas.monitoring import AttendanceStatus


@dataclass(frozen=True)
class PolicySpec:
    work_start_minute: int
    work_end_minute: int
    buffer_minutes: int
    regularization_window_minutes: int
    full_day_min_minutes: int
    half_day_min_minutes: int
    monthly_regularizations: int
    working_days_per_week: int
    timezone: str
    full_day_grace_minutes: int = 0

    @property
    def on_time_cutoff(self) -> int:
        return self.work_start_minute + self.buffer_minutes

    @property
    def regularizable_cutoff(self) -> int:
        return self.on_time_cutoff + self.regularization_window_minutes

    @property
    def full_day_hours_cutoff(self) -> int:
        """Worked-minute bar for a full day, after the grace band.

        Mirrors the arrival `buffer_minutes`: the hours dimension had no
        tolerance, so working one minute short of `full_day_min_minutes` fell
        straight to half day. The grace band gives it the same near-miss slack
        arrival already gets. Floored at 0 so a large grace can't invert it.
        """
        return max(0, self.full_day_min_minutes - self.full_day_grace_minutes)


@dataclass(frozen=True)
class DayVerdict:
    status: AttendanceStatus
    late_login: bool
    regularizable: bool  # arrived in the reg window and not yet regularized
    regularized: bool
    early_logout: bool
    arrival_minute: int | None  # local minutes from midnight, for display


def _local_minute(when: datetime, tz: str) -> int:
    aware = when if when.tzinfo else when.replace(tzinfo=UTC)
    local = aware.astimezone(ZoneInfo(tz))
    return local.hour * 60 + local.minute


def classify_day(
    *,
    login_at: datetime | None,
    worked_minutes: int,
    regularized: bool,
    policy: PolicySpec,
    day_complete: bool = True,
) -> DayVerdict:
    """Classify one employee-day against the policy.

    `day_complete` is False while the day is still running (it's today and the
    office window hasn't closed yet). Hours-based outcomes — the too-few-hours
    half-day and the early-logout flag — only apply once the day is complete:
    mid-day nobody has yet put in a full day's hours, so an on-time person who's
    still working shows as PRESENT (on track), not a premature HALF_DAY. Arrival
    facts (arriving after the reg window) still count immediately.
    """
    if login_at is None:
        return DayVerdict(AttendanceStatus.ABSENT, False, False, regularized, False, None)

    arrival = _local_minute(login_at, policy.timezone)
    on_time = arrival <= policy.on_time_cutoff
    in_reg_window = policy.on_time_cutoff < arrival <= policy.regularizable_cutoff
    too_late = arrival > policy.regularizable_cutoff
    hours_ok = worked_minutes >= policy.full_day_hours_cutoff
    # Worked hours only judge someone once the day is over (see docstring).
    too_few_hours = day_complete and worked_minutes < policy.half_day_min_minutes
    early_logout = day_complete and worked_minutes < policy.full_day_hours_cutoff

    arrival_ok = on_time or regularized

    if too_late and not regularized:
        status = AttendanceStatus.HALF_DAY
    elif too_few_hours:
        status = AttendanceStatus.HALF_DAY
    elif arrival_ok and hours_ok:
        status = AttendanceStatus.FULL_DAY
    elif arrival_ok and not day_complete:
        status = AttendanceStatus.PRESENT  # on time / regularized, day still running
    elif in_reg_window and not regularized:
        status = AttendanceStatus.LATE  # eligible to regularize → full day on approval
    else:
        status = AttendanceStatus.HALF_DAY

    return DayVerdict(
        status=status,
        late_login=not on_time,
        regularizable=in_reg_window and not regularized,
        regularized=regularized,
        early_logout=early_logout,
        arrival_minute=arrival,
    )
