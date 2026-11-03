"""Moving biometric attendance between two people enrolled under each other's
device id.

The punches and times are correct - Avora matched every one to the id it was
told. Only the name on them is wrong, so the repair moves ownership and has to
leave every recorded time exactly as the device reported it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from scripts.reattribute_biometric import (
    _bounds,
    load_biometric_sessions,
    merge_same_day,
    move_owners,
    swap_owners,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmployeeStatus, Role
from app.models.work_session import WorkSession

TZ = ZoneInfo("Asia/Kolkata")


async def _person(db: AsyncSession, email: str) -> uuid.UUID:
    """Returns the id, not the row: a commit expires the instance and reading an
    attribute afterwards lazy-loads outside the greenlet (MissingGreenlet)."""
    person = Employee(
        hr_external_id=f"hr-{uuid.uuid4().hex[:8]}",
        work_email=email,
        full_name=email.split("@")[0].title(),
        role=Role.EXECUTIVE,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.flush()
    return person.id


def _same(stored: datetime | None, expected: datetime | None) -> bool:
    """SQLite hands back naive datetimes; compare them as the UTC they are."""
    if stored is None or expected is None:
        return stored is expected
    left = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    return left == expected


def _utc(day: str, hhmm: str) -> datetime:
    """A local office time as the UTC instant the column stores."""
    y, m, d = (int(part) for part in day.split("-"))
    hh, mm = (int(part) for part in hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=TZ).astimezone(UTC)


async def _punch(
    db: AsyncSession,
    owner: uuid.UUID,
    day: str,
    start: str,
    end: str,
    source: str = "biometric",
) -> uuid.UUID:
    row = WorkSession(
        employee_id=owner,
        clock_in_at=_utc(day, start),
        clock_out_at=_utc(day, end),
        source=source,
        clock_out_source=source,
    )
    db.add(row)
    await db.flush()
    return row.id


def test_bounds_cover_the_whole_local_end_day() -> None:
    """An 18:00 IST punch on the last day is inside the range. IST is UTC+5:30,
    so a naive UTC end-of-day would cut the final evening off."""
    begin, stop = _bounds(date(2026, 10, 5), date(2026, 10, 31), TZ)
    assert begin <= _utc("2026-10-05", "08:00") < stop
    assert begin <= _utc("2026-10-31", "18:00") < stop
    assert _utc("2026-11-01", "09:00") >= stop
    assert _utc("2026-10-04", "17:00") < begin


async def test_swap_moves_ownership_and_keeps_every_time(db: AsyncSession) -> None:
    supriya = await _person(db, "supriya@corp.test")
    ashwin = await _person(db, "ashwin@corp.test")
    # What the crossed enrollment produced: each holds the other's day.
    on_supriya = await _punch(db, supriya, "2026-10-09", "08:53", "17:14")
    on_ashwin = await _punch(db, ashwin, "2026-10-09", "11:57", "17:42")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 5), date(2026, 10, 31), TZ)
    rows = await load_biometric_sessions(db, [supriya, ashwin], begin, stop)
    assert len(rows) == 2
    before = {row.id: (row.clock_in_at, row.clock_out_at) for row in rows}

    swap_owners(rows, supriya, ashwin)
    await db.commit()
    db.expire_all()

    moved_a = await db.get(WorkSession, on_supriya)
    moved_b = await db.get(WorkSession, on_ashwin)
    assert moved_a is not None and moved_b is not None
    assert moved_a.employee_id == ashwin
    assert moved_b.employee_id == supriya
    # The times are the device's record and must survive the move untouched.
    for row in (moved_a, moved_b):
        assert (row.clock_in_at, row.clock_out_at) == before[row.id]


async def test_swapping_twice_restores_the_original(db: AsyncSession) -> None:
    """The operation is its own inverse - that is the undo for a wrong range."""
    one = await _person(db, "one@corp.test")
    two = await _person(db, "two@corp.test")
    punched = await _punch(db, one, "2026-10-12", "09:08", "18:10")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 1), date(2026, 10, 31), TZ)
    for _ in range(2):
        rows = await load_biometric_sessions(db, [one, two], begin, stop)
        swap_owners(rows, one, two)
        await db.commit()

    db.expire_all()
    restored = await db.get(WorkSession, punched)
    assert restored is not None and restored.employee_id == one


async def test_only_biometric_sessions_move(db: AsyncSession) -> None:
    """A manual clock-in or the laptop agent's record was made by the person it
    names, so it stays where it is."""
    one = await _person(db, "three@corp.test")
    two = await _person(db, "four@corp.test")
    manual = await _punch(db, one, "2026-10-12", "09:00", "17:00", source="dashboard")
    punched = await _punch(db, one, "2026-10-13", "09:00", "17:00")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 1), date(2026, 10, 31), TZ)
    rows = await load_biometric_sessions(db, [one, two], begin, stop)
    assert {row.id for row in rows} == {punched}

    swap_owners(rows, one, two)
    await db.commit()
    db.expire_all()

    untouched = await db.get(WorkSession, manual)
    assert untouched is not None and untouched.employee_id == one


async def test_sessions_outside_the_range_are_left_alone(db: AsyncSession) -> None:
    one = await _person(db, "five@corp.test")
    two = await _person(db, "six@corp.test")
    september = await _punch(db, one, "2026-09-30", "09:00", "17:00")
    october = await _punch(db, one, "2026-10-05", "09:00", "17:00")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 5), date(2026, 10, 31), TZ)
    rows = await load_biometric_sessions(db, [one, two], begin, stop)
    assert {row.id for row in rows} == {october}

    swap_owners(rows, one, two)
    await db.commit()
    db.expire_all()

    kept = await db.get(WorkSession, september)
    assert kept is not None and kept.employee_id == one


async def test_nothing_in_range_is_a_no_op(db: AsyncSession) -> None:
    one = await _person(db, "seven@corp.test")
    two = await _person(db, "eight@corp.test")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 1), date(2026, 10, 31), TZ)
    assert await load_biometric_sessions(db, [one, two], begin, stop) == []
    assert list((await db.scalars(select(WorkSession))).all()) == []


async def test_move_is_one_way_and_merges_the_split_day(db: AsyncSession) -> None:
    """The shape left behind by correcting device ids mid-afternoon.

    One person's morning punch had already landed on the wrong record; their
    evening punch, arriving after the correction, opened a SECOND session on the
    right one. A swap would hand the evening back to the wrong person. A one-way
    move plus a merge gives the whole day to its owner and leaves the other with
    nothing - which is correct, because they were not in that day.
    """
    supriya = await _person(db, "supriya2@corp.test")
    ashwin = await _person(db, "ashwin2@corp.test")
    morning = await _punch(db, supriya, "2026-10-31", "09:04", "09:04")
    evening = await _punch(db, ashwin, "2026-10-31", "17:06", "17:06")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 31), date(2026, 10, 31), TZ)
    rows = await load_biometric_sessions(db, [supriya, ashwin], begin, stop)
    move_owners(rows, supriya, ashwin)
    redundant = merge_same_day(rows, TZ)
    for row in redundant:
        await db.delete(row)
    await db.commit()
    db.expire_all()

    kept = await db.get(WorkSession, morning)
    assert kept is not None
    assert kept.employee_id == ashwin
    assert _same(kept.clock_in_at, _utc("2026-10-31", "09:04"))
    assert _same(kept.clock_out_at, _utc("2026-10-31", "17:06"))  # evening folded in

    assert await db.get(WorkSession, evening) is None  # the stub is gone
    left = await db.scalars(select(WorkSession).where(WorkSession.employee_id == supriya))
    assert list(left.all()) == []  # she was not in that day


async def test_move_leaves_the_destinations_other_days_alone(db: AsyncSession) -> None:
    one = await _person(db, "nine@corp.test")
    two = await _person(db, "ten@corp.test")
    theirs = await _punch(db, two, "2026-10-29", "09:00", "17:00")
    await _punch(db, one, "2026-10-30", "09:10", "17:10")
    await db.commit()

    begin, stop = _bounds(date(2026, 10, 29), date(2026, 10, 31), TZ)
    rows = await load_biometric_sessions(db, [one, two], begin, stop)
    move_owners(rows, one, two)
    assert merge_same_day(rows, TZ) == []  # different days, nothing to merge
    await db.commit()
    db.expire_all()

    untouched = await db.get(WorkSession, theirs)
    assert untouched is not None
    assert untouched.employee_id == two
    assert _same(untouched.clock_in_at, _utc("2026-10-29", "09:00"))
    owned = await db.scalars(select(WorkSession).where(WorkSession.employee_id == two))
    assert len(list(owned.all())) == 2  # both days now theirs


def test_merge_keeps_the_latest_clock_out_and_an_open_day_stays_open() -> None:
    """An open session merged with a closed one takes the closed one's end; two
    open ones stay open rather than inventing a departure."""
    owner = uuid.uuid4()
    open_am = WorkSession(
        employee_id=owner,
        clock_in_at=_utc("2026-10-31", "09:04"),
        clock_out_at=None,
        source="biometric",
    )
    closed_pm = WorkSession(
        employee_id=owner,
        clock_in_at=_utc("2026-10-31", "17:06"),
        clock_out_at=_utc("2026-10-31", "17:06"),
        source="biometric",
    )
    redundant = merge_same_day([open_am, closed_pm], TZ)
    assert redundant == [closed_pm]
    assert _same(open_am.clock_out_at, _utc("2026-10-31", "17:06"))
    assert open_am.clock_out_source == "biometric"

    both_open = [
        WorkSession(
            employee_id=owner,
            clock_in_at=_utc("2026-11-01", "09:00"),
            clock_out_at=None,
            source="biometric",
        ),
        WorkSession(
            employee_id=owner,
            clock_in_at=_utc("2026-11-01", "11:00"),
            clock_out_at=None,
            source="biometric",
        ),
    ]
    assert len(merge_same_day(both_open, TZ)) == 1
    assert both_open[0].clock_out_at is None
