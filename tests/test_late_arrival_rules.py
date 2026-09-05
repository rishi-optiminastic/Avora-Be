"""Late-arrival rules from the Attendance Guidelines 2026.

Two rules, both about arriving after the 9:30 grace:

  - "required to complete 8 working hours from their actual time of arrival" —
    a stricter bar than an on-time day, because being late does not shorten the
    day owed. Late people used to owe LESS in practice, since the normal bar
    assumes a 9 AM start.
  - "If an employee reports after 9:30 AM more than 3 times in a month, any
    subsequent late arrival beyond the permitted 3 instances will be considered
    as a half-day." The first three stay full days on hours.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.attendance import AttendanceStatus, PolicySpec, classify_day

_TZ = "Asia/Kolkata"


def _policy() -> PolicySpec:
    return PolicySpec(
        work_start_minute=9 * 60,
        work_end_minute=17 * 60,
        buffer_minutes=30,  # on time until 9:30
        regularization_window_minutes=15,
        full_day_min_minutes=420,
        half_day_min_minutes=210,
        monthly_regularizations=2,
        working_days_per_week=6,
        timezone=_TZ,
        full_day_grace_minutes=30,
    )


def _at(hour: int, minute: int) -> datetime:
    """A local IST arrival time, as the UTC instant we would have stored."""
    return datetime(2026, 9, 10, hour, minute, tzinfo=ZoneInfo(_TZ)).astimezone(UTC)


def test_a_late_arrival_owes_eight_hours_not_the_normal_day() -> None:
    """In at 10:00. Seven hours would clear an on-time day; it does not clear this."""
    seven_hours = classify_day(
        login_at=_at(10, 0), worked_minutes=420, regularized=False, policy=_policy()
    )
    assert seven_hours.status is not AttendanceStatus.FULL_DAY
    assert seven_hours.late_login is True

    eight_hours = classify_day(
        login_at=_at(10, 0), worked_minutes=480, regularized=False, policy=_policy()
    )
    assert eight_hours.status is AttendanceStatus.FULL_DAY


def test_an_on_time_arrival_still_owes_only_the_normal_day() -> None:
    """The 8-hour rule is a penalty for lateness, not a raise for everyone."""
    v = classify_day(login_at=_at(9, 15), worked_minutes=400, regularized=False, policy=_policy())
    assert v.late_login is False
    assert v.status is AttendanceStatus.FULL_DAY


def test_the_first_three_lates_are_not_half_days() -> None:
    for already in (0, 1, 2):
        v = classify_day(
            login_at=_at(9, 45),
            worked_minutes=480,
            regularized=False,
            policy=_policy(),
            prior_lates_this_month=already,
        )
        assert v.status is AttendanceStatus.FULL_DAY, f"late #{already + 1} should still be full"
        assert v.regularizable is False  # nothing to contest — it counted as full
        assert v.late_login is True


def test_the_fourth_late_is_a_half_day_however_many_hours_are_worked() -> None:
    v = classify_day(
        login_at=_at(9, 45),
        worked_minutes=600,  # ten hours, and it still does not help
        regularized=False,
        policy=_policy(),
        prior_lates_this_month=3,
    )
    assert v.status is AttendanceStatus.HALF_DAY
    assert v.regularizable is True  # they can still contest it


def test_regularisation_overrides_the_allowance() -> None:
    """HR approving a specific day is a deliberate decision and outranks the count
    — otherwise a genuine missed punch could never be corrected once the
    allowance was spent."""
    v = classify_day(
        login_at=_at(11, 0),
        worked_minutes=480,
        regularized=True,
        policy=_policy(),
        prior_lates_this_month=9,
    )
    assert v.status is AttendanceStatus.FULL_DAY


def test_the_allowance_does_not_touch_someone_who_is_never_late() -> None:
    v = classify_day(
        login_at=_at(9, 0),
        worked_minutes=430,
        regularized=False,
        policy=_policy(),
        prior_lates_this_month=8,
    )
    assert v.status is AttendanceStatus.FULL_DAY
