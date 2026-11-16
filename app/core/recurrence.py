"""When a recurring task is next due. Pure, so every rule is unit-tested directly.

`Task.cadence` has existed since the tasks module shipped and nothing ever read
it: 509 live tasks say DAILY, WEEKLY or MONTHLY and not one has ever produced a
second instance. People have been ticking "daily" and re-creating the task by
hand every morning. This module is the arithmetic that fixes that.

The rules, and why each is the way it is:

* **The next instance is due relative to the LAST due date, not to today.** A
  weekly task due Monday that nobody generated for three weeks is next due the
  coming Monday, not three weeks on from whenever the worker happened to run.
* **Never in the past.** Catching up is rolled forward in whole cadence steps
  until the date is today or later, so switching the worker on does not create
  a task that is instantly overdue, and an outage does not back-date anything.
* **Monthly clamps to the end of a short month.** The 31st of January recurs on
  28 February, not 3 March: a month-end report belongs at month end.
* **A daily task skips days the office is shut.** A "daily" task on Sunday,
  or on Diwali, is noise. The working week comes from `AttendancePolicy` and
  the closed days from `holidays` - both already exist, so neither is
  invented here. Weekly and monthly are NOT skipped, because moving a weekly
  task off its agreed day changes what it means.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.models.task import TaskCadence


@dataclass(frozen=True)
class WorkingCalendar:
    """When the office is open, for the days a daily task may land on.

    Bundled rather than passed as two arguments because they answer one
    question together: a date is workable if it is a working weekday AND not
    a closed day. Splitting them let a caller supply one and silently forget
    the other.

    `weekdays` holds `date.weekday()` values. `closed` holds specific dates -
    public and company holidays. OPTIONAL holidays are deliberately NOT in
    it: the office is open, some people are in, and skipping work on a day
    most of the team is present would be the wrong default.
    """

    weekdays: frozenset[int]
    closed: frozenset[date] = field(default_factory=frozenset)

    def is_open(self, day: date) -> bool:
        return day.weekday() in self.weekdays and day not in self.closed

    def minus(self, days: frozenset[date]) -> WorkingCalendar:
        """The same calendar with more days shut.

        Used to lay one person's approved leave over the org's calendar, so
        a daily task does not generate onto a day its assignee is away. The
        org calendar is loaded once per sweep and this is cheap per task.
        """
        if not days:
            return self
        return WorkingCalendar(weekdays=self.weekdays, closed=self.closed | days)


#: Cadences that actually recur. ONE_TIME is the default and the majority.
RECURRING = frozenset({TaskCadence.DAILY, TaskCadence.WEEKLY, TaskCadence.MONTHLY})

#: How far a catch-up will roll forward before giving up. A task whose last due
#: date is years old is a data problem, not a schedule, and spinning through
#: thousands of iterations to reach today would hide that.
MAX_CATCH_UP_STEPS = 500


def add_months(day: date, months: int) -> date:
    """`day` plus whole months, clamped to the end of a short month.

    31 Jan + 1 month is 28 Feb (or the 29th), never 2 or 3 March. A month-end
    task that drifted into the next month would stop being a month-end task.
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1])
    )


def step_once(day: date, cadence: TaskCadence) -> date:
    """One cadence step forward from `day`."""
    if cadence is TaskCadence.DAILY:
        return day + timedelta(days=1)
    if cadence is TaskCadence.WEEKLY:
        return day + timedelta(days=7)
    if cadence is TaskCadence.MONTHLY:
        return add_months(day, 1)
    raise ValueError(f"{cadence} does not recur")


def next_due(
    last_due: date,
    cadence: TaskCadence,
    today: date,
    calendar_: WorkingCalendar | None = None,
) -> date | None:
    """The next due date at or after `today`, or None when it cannot be found.

    Relative to `last_due` so the series keeps its agreed day, then rolled
    forward in whole steps until it reaches today - which is what stops a
    switch-on or an outage from creating something already overdue.

    `calendar_` applies to DAILY only. A daily task landing on a Sunday or a
    public holiday moves to the next open day; a weekly one does not,
    because its day IS the agreement.
    """
    if cadence not in RECURRING:
        return None

    due = step_once(last_due, cadence)
    steps = 0
    while due < today:
        if steps >= MAX_CATCH_UP_STEPS:
            # A series this far behind is a data problem. Returning None puts
            # it in front of a human rather than silently inventing a date.
            return None
        due = step_once(due, cadence)
        steps += 1

    if cadence is TaskCadence.DAILY and calendar_ is not None:
        # A fortnight, not a week: a working week plus a run of public
        # holidays around Diwali or Christmas can close the office for
        # longer than seven days.
        for _ in range(14):
            if calendar_.is_open(due):
                return due
            due = due + timedelta(days=1)
        # Shut for a fortnight means a misconfigured policy, not a holiday.
        # Generating onto a closed day anyway would be worse than not.
        return None
    return due
