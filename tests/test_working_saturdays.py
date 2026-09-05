"""Alternate Saturdays are weekly offs, from the policy's effective date.

Attendance Guidelines 2026: "1st and 3rd Saturdays will be working days, while
2nd and 4th Saturdays will be weekly offs." The policy is effective 7 September
2026, so days before it keep the rule they were actually worked under — turning
past Saturdays into weekly offs would restate attendance people have already
seen and shift payroll months already processed.

Attendance, monitoring and payroll each used to answer "is this a working day?"
with their own `weekday() < working_days_per_week`. Adding the rule to one would
have left the others marking people ABSENT on their own weekly off, so they all
route through one helper now.
"""

from __future__ import annotations

import calendar
import inspect
from datetime import date

from app.core.payroll import (
    POLICY_EFFECTIVE_DATE,
    is_working_day,
    is_working_saturday,
    weekdays_in_month,
    working_days_between,
)
from app.services import attendance_service, monitoring_gate


def _saturdays(year: int, month: int) -> list[date]:
    last = calendar.monthrange(year, month)[1]
    return [d for day in range(1, last + 1) if (d := date(year, month, day)).weekday() == 5]


def test_the_second_and_fourth_saturday_are_off() -> None:
    # October 2026 has five Saturdays: 3, 10, 17, 24, 31.
    worked = [d.day for d in _saturdays(2026, 10) if is_working_saturday(d)]
    assert worked == [3, 17, 31]  # the 2nd (10th) and 4th (24th) are off


def test_a_fifth_saturday_stays_a_working_day() -> None:
    """The policy names only the 2nd and 4th, so a 5th is worked like the 1st."""
    assert is_working_saturday(date(2026, 10, 31))


def test_nothing_before_the_effective_date_changes() -> None:
    """August was worked under the old rule and is already paid. Every Saturday
    in it must still count, or attendance and payroll get restated behind people."""
    for saturday in _saturdays(2026, 8):
        assert is_working_day(saturday, 6) is True
    assert len(weekdays_in_month(2026, 8, 6)) == 26  # unchanged


def test_the_rule_starts_exactly_on_the_effective_date() -> None:
    assert POLICY_EFFECTIVE_DATE == date(2026, 9, 7)
    # 5 Sep is the 1st Saturday and worked either way; 12 Sep is the 2nd and is
    # the first Saturday the new rule actually removes.
    assert is_working_day(date(2026, 9, 5), 6) is True
    assert is_working_day(date(2026, 9, 12), 6) is False


def test_the_rule_only_applies_to_a_six_day_week() -> None:
    """Under Mon-Fri every Saturday is already off, so the rule is irrelevant."""
    for saturday in _saturdays(2026, 10):
        assert is_working_day(saturday, 6) == is_working_saturday(saturday)
        assert is_working_day(saturday, 5) is False
    assert is_working_day(date(2026, 10, 11), 6) is False  # Sunday, always off
    assert is_working_day(date(2026, 10, 12), 6) is True  # Monday


def test_the_month_count_drops_by_the_two_saturdays() -> None:
    """October 2026 is 27 Mon-Sat days; two of them are weekly offs."""
    assert len(weekdays_in_month(2026, 10, 6)) == 25
    assert len(weekdays_in_month(2026, 11, 6)) == 23  # 25 Mon-Sat, less two
    # A five-day week is untouched by the change.
    assert len(weekdays_in_month(2026, 10, 5)) == 22


def test_the_frontend_counter_agrees_with_this_one() -> None:
    """The apply dialog quotes a day count before the backend charges one, and it
    used to hardcode Mon-Fri — so under the six-day week it promised 2 days where
    the balance lost 3. The mirrored logic lives in fe/features/leaves/types.ts
    (`isWorkingDay`); these are the spans that expose any difference.
    """
    assert working_days_between(date(2026, 10, 8), date(2026, 10, 11), set(), 6) == 2
    assert working_days_between(date(2026, 10, 15), date(2026, 10, 18), set(), 6) == 3
    assert working_days_between(date(2026, 10, 1), date(2026, 10, 31), set(), 6) == 25
    assert working_days_between(date(2026, 11, 1), date(2026, 11, 30), set(), 6) == 23


def test_a_leave_over_a_weekly_off_saturday_costs_less_than_one_over_a_working_one() -> None:
    """The same four calendar days cost different amounts depending on which
    Saturday they cross — which is exactly why the two counters must agree."""
    over_off = working_days_between(date(2026, 10, 8), date(2026, 10, 11), set(), 6)
    over_worked = working_days_between(date(2026, 10, 15), date(2026, 10, 18), set(), 6)
    assert over_worked == over_off + 1


def test_attendance_and_monitoring_use_the_shared_rule() -> None:
    """The guard that matters: a service reintroducing its own weekday test would
    silently disagree with payroll about who was absent."""
    for module in (attendance_service, monitoring_gate):
        source = inspect.getsource(module)
        assert "is_working_day" in source, f"{module.__name__} no longer uses the shared rule"
        assert "weekday() <" not in source, (
            f"{module.__name__} decides working days on its own again"
        )
        assert "weekday() >=" not in source, (
            f"{module.__name__} decides working days on its own again"
        )
