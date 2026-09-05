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
    # Attendance Guidelines 2026: a late arrival must "complete 8 working hours
    # from their actual time of arrival" — a stricter bar than the normal day,
    # because arriving late does not shorten the day owed.
    late_full_day_minutes: int = 480
    # "If an employee reports after 9:30 AM more than 3 times in a month, any
    # subsequent late arrival beyond the permitted 3 instances will be considered
    # as a half-day." The first three are late but still full days on hours.
    monthly_late_allowance: int = 3

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


# Day outcomes an employee may ask to have corrected. A clean full day (or one
# still in progress and on track) has nothing to contest.
_CONTESTABLE = frozenset(
    {AttendanceStatus.LATE, AttendanceStatus.HALF_DAY, AttendanceStatus.ABSENT}
)


@dataclass(frozen=True)
class DayVerdict:
    status: AttendanceStatus
    late_login: bool
    regularizable: bool  # arrived in the reg window and not yet regularized
    regularized: bool
    early_logout: bool
    arrival_minute: int | None  # local minutes from midnight, for display


def local_minute(when: datetime, tz: str) -> int:
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
    prior_lates_this_month: int = 0,
) -> DayVerdict:
    """Classify one employee-day against the policy.

    `day_complete` is False while the day is still running (it's today and the
    office window hasn't closed yet). Hours-based outcomes — the too-few-hours
    half-day and the early-logout flag — only apply once the day is complete:
    mid-day nobody has yet put in a full day's hours, so an on-time person who's
    still working shows as PRESENT (on track), not a premature HALF_DAY. Arrival
    facts (arriving after the reg window) still count immediately.

    `prior_lates_this_month` is how many times this person already arrived late
    earlier in the same month. The policy allows three; from the fourth onward a
    late arrival is a half day however many hours are worked. Regularisation
    still overrides it — that is a deliberate HR decision about a specific day,
    not an excuse the employee grants themselves.
    """
    if login_at is None:
        # No punch at all. This is the case people MOST need to contest — a missed
        # biometric scan, a forgotten clock-in — so it is regularizable, not a dead
        # end. It was previously excluded, which is why "the option isn't there"
        # was reported by exactly the people who needed it.
        return DayVerdict(
            AttendanceStatus.ABSENT, False, not regularized, regularized, False, None
        )

    arrival = local_minute(login_at, policy.timezone)
    on_time = arrival <= policy.on_time_cutoff
    # A late arrival owes a full 8 hours from when they actually got in; an
    # on-time one owes the normal day. Late people used to owe LESS in practice,
    # because the normal bar assumed a 9 AM start.
    required_minutes = (
        policy.late_full_day_minutes if not on_time else policy.full_day_hours_cutoff
    )
    hours_ok = worked_minutes >= required_minutes
    # Worked hours only judge someone once the day is over (see docstring).
    too_few_hours = day_complete and worked_minutes < policy.half_day_min_minutes
    early_logout = day_complete and worked_minutes < required_minutes
    # Beyond the monthly allowance, being late is itself the half day.
    over_late_allowance = not on_time and prior_lates_this_month >= policy.monthly_late_allowance

    if over_late_allowance and not regularized:
        # Past the monthly allowance, the lateness itself is the half day.
        status = AttendanceStatus.HALF_DAY
    elif too_few_hours:
        status = AttendanceStatus.HALF_DAY
    elif hours_ok:
        # Met the hours owed — the normal day when on time, a full 8 from arrival
        # when late. The policy penalises lateness by owing MORE, not by capping
        # the day at half however long you stay; a hard arrival cutoff used to
        # make an 11 AM start unsalvageable even after ten hours of work.
        status = AttendanceStatus.FULL_DAY
    elif regularized:
        status = AttendanceStatus.FULL_DAY  # HR approved this specific day
    elif not day_complete:
        status = AttendanceStatus.PRESENT  # still running, hours not final yet
    elif not on_time:
        status = AttendanceStatus.LATE  # eligible to regularize → full on approval
    else:
        status = AttendanceStatus.HALF_DAY

    return DayVerdict(
        status=status,
        late_login=not on_time,
        # Any day that didn't land clean can be contested; the monthly credit cap
        # (enforced on approval) is what limits abuse, not an arbitrarily narrow
        # arrival window. Restricting this to `in_reg_window` meant someone who
        # arrived an hour late — or worked too few hours — had no way to ask,
        # while someone twenty minutes late did.
        regularizable=not regularized and status in _CONTESTABLE,
        regularized=regularized,
        early_logout=early_logout,
        arrival_minute=arrival,
    )
