"""When a recurring task is next due - the rules, tested directly.

Pure functions, so these are exhaustive about the dates that decide whether a
series keeps its agreed day: month ends, outages, and weekends.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.recurrence import (
    MAX_CATCH_UP_STEPS,
    WorkingCalendar,
    add_months,
    next_due,
    step_once,
)
from app.models.task import TaskCadence

#: Mon-Fri, the shape `AttendancePolicy` describes for a five-day week.
WEEKDAYS = frozenset({0, 1, 2, 3, 4})
#: An open office with no holidays, the common case.
OPEN = WorkingCalendar(weekdays=WEEKDAYS)


class TestAddMonths:
    def test_an_ordinary_month(self) -> None:
        assert add_months(date(2026, 3, 15), 1) == date(2026, 4, 15)

    def test_the_31st_clamps_to_the_end_of_a_short_month(self) -> None:
        """A month-end report belongs at month end, not drifting into March."""
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_a_leap_february(self) -> None:
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_it_rolls_over_the_year(self) -> None:
        assert add_months(date(2026, 12, 10), 1) == date(2027, 1, 10)

    def test_clamping_does_not_stick(self) -> None:
        # 31 Jan -> 28 Feb -> 28 Mar is the honest reading of "monthly from a
        # date": the series keeps the day it can, and a clamp is not a new
        # agreement. Worth pinning either way so a change is deliberate.
        assert add_months(add_months(date(2026, 1, 31), 1), 1) == date(2026, 3, 28)


class TestStepOnce:
    @pytest.mark.parametrize(
        ("cadence", "expected"),
        [
            (TaskCadence.DAILY, date(2026, 11, 13)),
            (TaskCadence.WEEKLY, date(2026, 11, 19)),
            (TaskCadence.MONTHLY, date(2026, 12, 12)),
        ],
    )
    def test_each_cadence_steps_by_its_own_unit(self, cadence: TaskCadence, expected: date) -> None:
        assert step_once(date(2026, 11, 12), cadence) == expected

    def test_a_one_time_task_cannot_be_stepped(self) -> None:
        with pytest.raises(ValueError, match="does not recur"):
            step_once(date(2026, 11, 12), TaskCadence.ONE_TIME)


class TestNextDue:
    def test_a_one_time_task_never_recurs(self) -> None:
        assert next_due(date(2026, 11, 12), TaskCadence.ONE_TIME, date(2026, 11, 12)) is None

    def test_the_next_one_is_relative_to_the_last_due_date(self) -> None:
        """Not to today: a weekly task keeps the weekday it was agreed on."""
        # Last due Monday 9 Nov, today Wednesday 11 Nov -> Monday 16 Nov.
        assert next_due(date(2026, 11, 9), TaskCadence.WEEKLY, date(2026, 11, 11)) == date(
            2026, 11, 16
        )

    def test_a_series_behind_by_weeks_catches_up_to_the_right_weekday(self) -> None:
        """Three weeks of downtime produces ONE task, on the agreed day - not
        three, and not a date three weeks from whenever the worker ran."""
        due = next_due(date(2026, 10, 5), TaskCadence.WEEKLY, date(2026, 11, 11))

        assert due == date(2026, 11, 16)
        assert due.weekday() == date(2026, 10, 5).weekday(), "still a Monday"

    def test_it_is_never_created_already_overdue(self) -> None:
        # Switching the worker on against a year-old task must not back-date.
        due = next_due(date(2026, 1, 5), TaskCadence.MONTHLY, date(2026, 11, 12))
        assert due is not None and due >= date(2026, 11, 12)

    def test_a_due_date_in_the_future_is_left_alone(self) -> None:
        """Nothing to catch up: one step past the last one, and that is it."""
        assert next_due(date(2026, 12, 1), TaskCadence.DAILY, date(2026, 11, 12)) == date(
            2026, 12, 2
        )

    def test_a_series_absurdly_far_behind_gives_up_rather_than_guessing(self) -> None:
        """A task last due in 2015 is a data problem. Returning None puts it
        in front of a human instead of inventing a date."""
        assert next_due(date(2015, 1, 1), TaskCadence.DAILY, date(2026, 11, 12)) is None

    def test_the_catch_up_limit_is_generous_enough_for_real_downtime(self) -> None:
        # 500 daily steps is well over a year - longer than any outage that
        # should be handled by quietly resuming.
        assert MAX_CATCH_UP_STEPS >= 365


class TestWorkingDays:
    def test_a_daily_task_skips_the_weekend(self) -> None:
        """Friday 13 Nov 2026 -> Monday 16th, not Saturday."""
        assert next_due(date(2026, 11, 13), TaskCadence.DAILY, date(2026, 11, 13), OPEN) == date(
            2026, 11, 16
        )

    def test_a_daily_task_on_a_working_day_is_not_moved(self) -> None:
        assert next_due(date(2026, 11, 10), TaskCadence.DAILY, date(2026, 11, 10), OPEN) == date(
            2026, 11, 11
        )

    def test_a_weekly_task_is_NOT_moved_off_its_day(self) -> None:
        """Its day is the agreement. A Saturday weekly task means Saturday."""
        saturday = date(2026, 11, 14)
        assert next_due(saturday, TaskCadence.WEEKLY, saturday, OPEN) == date(2026, 11, 21)

    def test_a_monthly_task_is_not_moved_either(self) -> None:
        assert next_due(date(2026, 10, 31), TaskCadence.MONTHLY, date(2026, 11, 1), OPEN) == date(
            2026, 11, 30
        )

    def test_an_org_with_no_working_days_generates_nothing(self) -> None:
        """A misconfigured policy must not quietly put work on every day."""
        assert (
            next_due(
                date(2026, 11, 12),
                TaskCadence.DAILY,
                date(2026, 11, 12),
                WorkingCalendar(weekdays=frozenset()),
            )
            is None
        )

    def test_a_seven_day_week_never_skips(self) -> None:
        every_day = WorkingCalendar(weekdays=frozenset(range(7)))
        assert next_due(
            date(2026, 11, 13), TaskCadence.DAILY, date(2026, 11, 13), every_day
        ) == date(2026, 11, 14)

    def test_without_a_policy_nothing_is_skipped(self) -> None:
        """`None` means "no opinion", which must not silently mean "weekdays"."""
        assert next_due(date(2026, 11, 13), TaskCadence.DAILY, date(2026, 11, 13)) == date(
            2026, 11, 14
        )


class TestHolidays:
    """A daily task on Diwali is as much noise as one on a Sunday."""

    def test_a_daily_task_skips_a_public_holiday(self) -> None:
        shut = WorkingCalendar(weekdays=WEEKDAYS, closed=frozenset({date(2026, 11, 12)}))

        assert next_due(date(2026, 11, 11), TaskCadence.DAILY, date(2026, 11, 11), shut) == date(
            2026, 11, 13
        )

    def test_it_skips_a_run_of_closed_days_into_the_next_working_day(self) -> None:
        # Thu 12th and Fri 13th shut, so the next open day is Monday 16th.
        shut = WorkingCalendar(
            weekdays=WEEKDAYS, closed=frozenset({date(2026, 11, 12), date(2026, 11, 13)})
        )

        assert next_due(date(2026, 11, 11), TaskCadence.DAILY, date(2026, 11, 11), shut) == date(
            2026, 11, 16
        )

    def test_a_weekly_task_is_NOT_moved_off_a_holiday(self) -> None:
        """Its day is the agreement, holiday or not. Moving it would silently
        change what "the Monday review" means."""
        shut = WorkingCalendar(weekdays=WEEKDAYS, closed=frozenset({date(2026, 11, 16)}))

        assert next_due(date(2026, 11, 9), TaskCadence.WEEKLY, date(2026, 11, 10), shut) == date(
            2026, 11, 16
        )

    def test_a_fortnight_of_closure_gives_up_rather_than_guessing(self) -> None:
        """Shut for two solid weeks is a misconfigured calendar, not a
        holiday - and landing work on a closed day anyway would be worse."""
        shut = WorkingCalendar(
            weekdays=WEEKDAYS,
            closed=frozenset(date(2026, 11, 12) + timedelta(days=i) for i in range(20)),
        )

        assert next_due(date(2026, 11, 11), TaskCadence.DAILY, date(2026, 11, 11), shut) is None

    def test_an_open_calendar_skips_nothing(self) -> None:
        assert next_due(date(2026, 11, 11), TaskCadence.DAILY, date(2026, 11, 11), OPEN) == date(
            2026, 11, 12
        )


class TestWorkingCalendar:
    def test_a_working_weekday_with_no_holiday_is_open(self) -> None:
        assert OPEN.is_open(date(2026, 11, 12)) is True

    def test_a_weekend_is_shut(self) -> None:
        assert OPEN.is_open(date(2026, 11, 14)) is False

    def test_a_holiday_on_a_weekday_is_shut(self) -> None:
        shut = WorkingCalendar(weekdays=WEEKDAYS, closed=frozenset({date(2026, 11, 12)}))
        assert shut.is_open(date(2026, 11, 12)) is False
