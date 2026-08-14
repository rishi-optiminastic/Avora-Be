"""Tenure banding — how long someone has been here, as a leave-entitlement tier.

Pure date arithmetic, no DB and no I/O, so it can be reasoned about and tested in
isolation (mirrors `core/payroll.py`). The band is always DERIVED from the hire
date, never stored on the employee: a stored value silently goes stale the day
someone crosses a boundary, and nobody notices until a leave balance is wrong.

Three bands:
  probation  — the first `probation_months` (default 6) after joining
  confirmed  — probation complete, still under a year
  tenured    — a year or more of service
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

# A leave year is a year — accrual can never credit more months than that.
MONTHS_IN_YEAR = 12


class TenureStatus(StrEnum):
    PROBATION = "probation"
    CONFIRMED = "confirmed"
    TENURED = "tenured"


def add_months(start: date, months: int) -> date:
    """`start` shifted by whole months, clamped to the end of a shorter month.

    Aug 31 + 6 months is Feb 28/29, not an invalid Feb 31 — the same clamping the
    leave-year window applies to a Feb-29 anniversary.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    # Step back from the 1st of the NEXT month to find this month's last day.
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(start.day, last_day))


def months_completed(start: date, today: date) -> int:
    """Whole months elapsed from `start` to `today` (0 before the first one).

    "Whole" means the day-of-month anniversary has been reached: Jan 15 → Feb 14
    is 0 months, Jan 15 → Feb 15 is 1. Negative spans clamp to 0 so a future hire
    date can never produce a negative entitlement.
    """
    if today <= start:
        return 0
    months = (today.year - start.year) * 12 + (today.month - start.month)
    if today.day < start.day:
        months -= 1
    return max(0, months)


def probation_end(hire_date: date, probation_months: int) -> date:
    """The first day on which someone counts as confirmed."""
    return add_months(hire_date, probation_months)


def tenure_status(hire_date: date, today: date, *, probation_months: int) -> TenureStatus:
    """Which entitlement band `hire_date` falls in as of `today`."""
    months = months_completed(hire_date, today)
    if months >= MONTHS_IN_YEAR:
        return TenureStatus.TENURED
    if months >= probation_months:
        return TenureStatus.CONFIRMED
    return TenureStatus.PROBATION


def accrued_units(accrual_start: date, today: date, *, per_month: float) -> float:
    """Entitlement accrued by `today`, credited UP FRONT each month.

    "One planned leave every month" credits the day the month begins, not the day
    it ends — so a newly-confirmed employee has a day to spend immediately rather
    than waiting a month for their first one. Hence `+ 1` on completed months.

    Capped at a year's worth: the caller resets `accrual_start` each leave year,
    and the cap keeps a stale start date from inventing unlimited days.
    """
    if today < accrual_start:
        return 0.0
    months = min(months_completed(accrual_start, today) + 1, MONTHS_IN_YEAR)
    return months * per_month
