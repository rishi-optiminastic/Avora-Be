"""Timezone helpers.

`as_utc` exists because SQLite - which the test suite runs on - hands back a
naive datetime from a `DateTime(timezone=True)` column, while Postgres hands
back an aware one. Code that does arithmetic on a value read from the database
therefore works in production and raises "can't subtract offset-naive and
offset-aware datetimes" in tests, or worse, silently compares a local wall clock
against UTC and lands a day out (the trap task recurrence hit).

Normalising on read makes both engines behave the same. Six services already
inline this; new code should call this instead of adding a seventh copy.
"""

from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """The same instant, as an aware UTC datetime.

    A naive value is ASSUMED to be UTC, which is true for anything this
    application stored (times are UTC internally, converted at the edge only -
    Coding conventions section 6).
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
