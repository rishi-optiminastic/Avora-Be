"""The company leave year: the Indian financial year, 1 April to 31 March.

Pure date arithmetic, shared by the balance engine and by the allocation
service (which stamps "taken before Avora" days with the leave year they belong
to). Everyone resets together on 1 April (Leave Policy 2026, General
Guidelines), rather than each person resetting on their own joining anniversary.
"""

from __future__ import annotations

from datetime import date, timedelta

LEAVE_YEAR_START_MONTH = 4


def leave_year_window(today: date) -> tuple[date, date]:
    """The leave window containing `today`, as [start, end] inclusive.

    January-March belong to the year that STARTED the previous April, so
    February 2027 sits in the 2026-27 window.
    """
    year = today.year if today.month >= LEAVE_YEAR_START_MONTH else today.year - 1
    start = date(year, LEAVE_YEAR_START_MONTH, 1)
    return start, date(year + 1, LEAVE_YEAR_START_MONTH, 1) - timedelta(days=1)
