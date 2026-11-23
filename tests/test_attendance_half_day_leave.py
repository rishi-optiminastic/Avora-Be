"""Half-day leave as its own attendance status.

Someone who takes approved half-day leave and works the rest of the day used to
read as a plain `half_day` - the same label as somebody who just put in too few
hours. HR was correcting it by hand every month. It now has its own status.

The sharp edge is money: before this, that day counted in `half_days`, which
payroll pays at 0.5, and the leave half arrived separately through
`_leave_days_by_employee` (which already weights HALF_DAY at 0.5). Moving the
day to a new status must not drop either half, so the present-day arithmetic is
pinned here as well as the label.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.leave import HalfDayPeriod, Leave, LeaveStatus, LeaveType
from app.models.work_session import WorkSession
from app.schemas.attendance_report import AttendanceMonthSummary
from app.services.payroll_service import _present_days
from tests.conftest import _Seed, auth_headers

_OFFICE = ZoneInfo("Asia/Kolkata")


def _recent_weekday() -> date:
    """LAST week's Monday - a past working day whose office hours have certainly
    closed, never a weekend, and (in the disposable test DB) never a holiday.

    Not this week's: when the suite runs on a Monday morning the day is still in
    progress, and an unfinished day classifies as PRESENT rather than HALF_DAY -
    which would make the "a short day is still half_day" test pass or fail
    depending on the weekday it ran.
    """
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday() + 7)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_OFFICE).astimezone(UTC)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def _worked(db: AsyncSession, employee: Employee, day: date, until: int) -> None:
    db.add(
        WorkSession(
            employee_id=employee.id,
            clock_in_at=_at(day, 9, 0),
            clock_out_at=_at(day, until, 0),
            source="dashboard",
        )
    )
    await db.commit()


async def _leave(
    db: AsyncSession, employee: Employee, day: date, kind: LeaveType = LeaveType.HALF_DAY
) -> None:
    db.add(
        Leave(
            employee_id=employee.id,
            leave_type=kind,
            start_date=_midnight(day),
            end_date=_midnight(day),
            half_day_period=HalfDayPeriod.SECOND_HALF if kind is LeaveType.HALF_DAY else None,
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()


async def _range_status(
    client: AsyncClient, settings: Settings, seed: _Seed, day: date
) -> str | None:
    iso = day.isoformat()
    resp = await client.get(
        f"/api/v1/attendance/range?start={iso}&end={iso}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    return None if not rows else str(rows[0]["status"])


# --- the label ---------------------------------------------------------------- #
async def test_a_worked_day_with_half_day_leave_is_its_own_status(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The bug: this read as `half_day`, indistinguishable from someone who
    simply left early without asking."""
    day = _recent_weekday()
    await _worked(db, seed.report, day, until=13)
    await _leave(db, seed.report, day)

    assert await _range_status(client, settings, seed, day) == "half_day_leave"


async def test_a_short_day_with_no_leave_behind_it_is_still_half_day(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The other half of the distinction. Without this the new status would just
    be a rename."""
    day = _recent_weekday()
    await _worked(db, seed.report, day, until=13)

    assert await _range_status(client, settings, seed, day) == "half_day"


async def test_a_full_day_of_leave_is_still_on_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday()
    await _leave(db, seed.report, day, kind=LeaveType.PLANNED)

    assert await _range_status(client, settings, seed, day) == "on_leave"


async def test_half_day_leave_with_no_session_at_all_stays_on_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Deliberate. With no session there is no worked half to credit, and this
    path feeds payroll's absence accounting - relabelling it would move money
    for a case nobody complained about."""
    day = _recent_weekday()
    await _leave(db, seed.report, day)

    assert await _range_status(client, settings, seed, day) == "on_leave"


async def test_the_single_day_board_agrees_with_the_range(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Two separate code paths derive this status; they must not drift."""
    day = _recent_weekday()
    await _worked(db, seed.report, day, until=13)
    await _leave(db, seed.report, day)

    resp = await client.get(
        f"/api/v1/attendance?date={day.isoformat()}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    assert rows and rows[0]["status"] == "half_day_leave"


# --- the money ----------------------------------------------------------------- #
def test_payroll_pays_a_half_day_leave_day_exactly_as_it_used_to() -> None:
    """A month of one such day must still be worth 0.5 present days. Before the
    status existed it landed in `half_days`; the two have to agree or every
    approved half-day leave quietly docks half a day's pay."""
    before = AttendanceMonthSummary(
        employee_id=uuid.uuid4(),
        full_days=0,
        half_days=1,
        late_days=0,
        absent_days=0,
        leave_days=0,
        regularized_days=0,
        worked_minutes=240,
    )
    after = before.model_copy(update={"half_days": 0, "half_day_leave_days": 1})

    assert _present_days(after) == _present_days(before) == 0.5


def test_half_day_leave_does_not_leak_into_the_other_counters() -> None:
    summary = AttendanceMonthSummary(
        employee_id=uuid.uuid4(),
        full_days=2,
        half_days=0,
        late_days=0,
        absent_days=0,
        leave_days=0,
        half_day_leave_days=2,
        regularized_days=0,
        worked_minutes=1200,
    )

    # 2 full + 2 worked halves. `leave_days` stays a count of WHOLE days off.
    assert _present_days(summary) == 3.0
    assert summary.leave_days == 0
    assert summary.half_days == 0


async def test_a_full_session_with_half_day_leave_still_reads_as_half_day_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Deliberate, and the case HR hits most: somebody takes the afternoon off
    but the session still runs to 6 because they never clocked out.

    The approved leave is the better evidence, so it wins over the hours. The
    day then credits 0.5 present instead of 1.0, which is also the more honest
    number - one calendar day cannot be worth 1.5 paid days. At month level
    payroll clamps with `min(elapsed_working, present + paid)` anyway, so a
    full attendance month is unchanged.
    """
    day = _recent_weekday()
    await _worked(db, seed.report, day, until=18)
    await _leave(db, seed.report, day)

    assert await _range_status(client, settings, seed, day) == "half_day_leave"


async def test_the_month_report_counts_it_separately(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday()
    await _worked(db, seed.report, day, until=13)
    await _leave(db, seed.report, day)

    resp = await client.get(
        f"/api/v1/attendance/report?month={day.strftime('%Y-%m')}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    assert rows, "the report should include the employee"
    assert rows[0]["half_day_leave_days"] == 1
    assert rows[0]["half_days"] == 0
    assert rows[0]["leave_days"] == 0
