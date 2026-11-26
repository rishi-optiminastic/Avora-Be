"""Which quota a half day actually spends from.

A HALF DAY ALWAYS COMES OFF SICK LEAVE, in every band. That is what HR has
always done by hand and what their balance sheet is built from.

It used to depend on the band - sick while planned was locked, planned once
confirmed. That was wrong in a way nobody saw until a quota was corrected: the
day somebody's planned leave was fixed, their half days silently moved off
sick and onto planned, changing two balances at once for a reason unrelated to
either. These tests pin the bucket to sick regardless of band, regardless of
any override, and regardless of when the day was taken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenure import add_months
from app.models.employee import Employee
from app.models.leave import HalfDayPeriod, Leave, LeaveStatus, LeaveType
from app.models.leave_allocation import LeaveAllocation
from tests.conftest import _Seed, auth_headers


def _dt(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _weekday(day: date) -> date:
    """`day`, pulled back to Friday if it lands on a weekend. Mon-Fri is a
    working day under every `working_days_per_week` setting, so these tests
    count the same whether the org works five days or six."""
    return day - timedelta(days=day.weekday() - 4) if day.weekday() > 4 else day


def _weekday_before(days_ago: int) -> date:
    return _weekday(datetime.now(UTC).date() - timedelta(days=days_ago))


def _weekday_ahead(days: int) -> date:
    """A future weekday. Pulled back, never pushed forward, so it cannot slip
    past a leave-year boundary the test is not thinking about."""
    return _weekday(datetime.now(UTC).date() + timedelta(days=days))


async def _set_hire_date(db: AsyncSession, employee_id: uuid.UUID, hire: date) -> None:
    person = await db.get(Employee, employee_id)
    assert person is not None
    person.hire_date = hire
    await db.commit()


async def _add_half_day(db: AsyncSession, employee_id: uuid.UUID, day: date) -> None:
    db.add(
        Leave(
            employee_id=employee_id,
            leave_type=LeaveType.HALF_DAY,
            start_date=_dt(day),
            end_date=_dt(day),
            half_day_period=HalfDayPeriod.FIRST_HALF,
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()


async def _balance(client: AsyncClient, settings: Settings, actor: Employee) -> dict[str, object]:
    resp = await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, actor))
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _row(payload: dict[str, object], leave_type: LeaveType) -> dict[str, float]:
    balances = payload["balances"]
    assert isinstance(balances, list)
    for row in balances:
        if row["leave_type"] == leave_type.value:
            return row  # type: ignore[no-any-return]
    raise AssertionError(f"{leave_type} missing from the balance")


# --- on probation: the day comes off sick ----------------------------------- #
async def test_a_probationers_half_day_is_deducted_from_sick_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The bug this file exists for. The day used to land in the locked planned
    bucket, so nothing the employee could see ever moved."""
    await _set_hire_date(db, seed.report.id, _weekday_before(30))
    await _add_half_day(db, seed.report.id, _weekday_before(7))

    payload = await _balance(client, settings, seed.report)

    assert payload["half_day_bucket"] == LeaveType.SICK.value
    sick = _row(payload, LeaveType.SICK)
    assert sick["allocated"] == 4.0
    assert sick["used"] == 0.5
    assert sick["remaining"] == 3.5


async def test_the_locked_planned_bucket_no_longer_absorbs_it(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Planned stays a clean 0 of 0. It used to read -0.5 remaining on a card
    the probationer is told they are not eligible for."""
    await _set_hire_date(db, seed.report.id, _weekday_before(30))
    await _add_half_day(db, seed.report.id, _weekday_before(7))

    planned = _row(await _balance(client, settings, seed.report), LeaveType.PLANNED)

    assert planned["allocated"] == 0.0
    assert planned["used"] == 0.0
    assert planned["remaining"] == 0.0


async def test_a_pending_half_day_is_reserved_against_sick_too(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _set_hire_date(db, seed.report.id, _weekday_before(30))
    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.HALF_DAY,
            start_date=_dt(_weekday_before(2)),
            end_date=_dt(_weekday_before(2)),
            half_day_period=HalfDayPeriod.FIRST_HALF,
            status=LeaveStatus.SUBMITTED,
        )
    )
    await db.commit()

    sick = _row(await _balance(client, settings, seed.report), LeaveType.SICK)

    assert sick["pending"] == 0.5
    assert sick["remaining"] == 3.5


# --- off probation: the day comes off planned ------------------------------- #
async def test_a_confirmed_employees_half_day_still_comes_off_sick(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Confirmation does not move half days onto planned leave."""
    await _set_hire_date(db, seed.report.id, add_months(datetime.now(UTC).date(), -7))
    await _add_half_day(db, seed.report.id, _weekday_before(3))

    payload = await _balance(client, settings, seed.report)

    assert payload["half_day_bucket"] == LeaveType.SICK.value
    assert _row(payload, LeaveType.SICK)["used"] == 0.5
    assert _row(payload, LeaveType.PLANNED)["used"] == 0.0


async def test_a_planned_override_does_not_pull_the_day_onto_planned(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Granting somebody planned leave must not move their half days.

    This is the case that forced the rule: correcting a quota silently
    re-pointed days already taken, so two balances moved for a reason that had
    nothing to do with either of them.
    """
    await _set_hire_date(db, seed.report.id, _weekday_before(30))
    db.add(LeaveAllocation(employee_id=seed.report.id, planned_days=3))
    await db.commit()
    await _add_half_day(db, seed.report.id, _weekday_before(7))

    payload = await _balance(client, settings, seed.report)

    assert payload["half_day_bucket"] == LeaveType.SICK.value
    assert _row(payload, LeaveType.SICK)["used"] == 0.5
    assert _row(payload, LeaveType.PLANNED)["used"] == 0.0


# --- the band on the DAY, not today's --------------------------------------- #
async def test_confirmation_does_not_refund_a_half_day_taken_on_probation(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Confirmed last month, half day taken two months ago while still on
    probation. Judging the bucket by today's band would move that day onto
    planned leave - handing back sick leave already spent and overdrawing a
    quota that did not exist when the day was taken."""
    hire = add_months(datetime.now(UTC).date(), -7)
    await _set_hire_date(db, seed.report.id, hire)
    await _add_half_day(db, seed.report.id, _weekday(add_months(datetime.now(UTC).date(), -2)))

    payload = await _balance(client, settings, seed.report)

    # Sick either way now: the bucket no longer depends on the band, so there
    # is nothing for confirmation to move.
    assert payload["half_day_bucket"] == LeaveType.SICK.value
    assert _row(payload, LeaveType.SICK)["used"] == 0.5
    assert _row(payload, LeaveType.PLANNED)["used"] == 0.0


# --- applying ---------------------------------------------------------------- #
async def _apply_half_day(
    client: AsyncClient, settings: Settings, actor: Employee, day: date
) -> tuple[int, str]:
    resp = await client.post(
        "/api/v1/leaves",
        headers=auth_headers(settings, actor),
        json={
            "leave_type": LeaveType.HALF_DAY.value,
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "half_day_period": HalfDayPeriod.FIRST_HALF.value,
            "reason": "appointment",
        },
    )
    return resp.status_code, resp.text


async def test_a_probationer_with_no_sick_leave_left_is_refused_a_half_day(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The point of charging it to a real quota: it can now run out. Before this
    the request was waved through against a bucket of nothing."""
    await _set_hire_date(db, seed.report.id, _weekday_before(60))
    db.add(LeaveAllocation(employee_id=seed.report.id, sick_days=0))
    await db.commit()

    status, body = await _apply_half_day(client, settings, seed.report, _weekday_ahead(3))

    assert status == 422, body
    assert "sick" in body.lower()


async def test_a_probationer_with_sick_leave_left_may_still_take_one(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Half-day is not withheld for tenure. Charging it to sick must not turn
    into a tenure gate - a probationer with sick days left keeps the day off."""
    await _set_hire_date(db, seed.report.id, _weekday_before(60))

    status, body = await _apply_half_day(client, settings, seed.report, _weekday_ahead(3))

    assert status == 201, body


async def test_the_probation_monthly_cap_still_applies_on_top_of_the_quota(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The cap used to stand IN PLACE OF a quota check. It now sits on top of
    one, so two half days in a month is still the limit even with sick days
    to spare."""
    await _set_hire_date(db, seed.report.id, _weekday_before(60))
    # Three distinct weekdays inside next month, so the month is unambiguous.
    nxt = (datetime.now(UTC).date().replace(day=1) + timedelta(days=32)).replace(day=1)
    days = [day for day in (nxt + timedelta(days=n) for n in range(10)) if day.weekday() < 5][:3]

    first, _ = await _apply_half_day(client, settings, seed.report, days[0])
    second, _ = await _apply_half_day(client, settings, seed.report, days[1])
    third, body = await _apply_half_day(client, settings, seed.report, days[2])

    assert (first, second) == (201, 201)
    assert third == 422, body
    assert "half day" in body.lower()
