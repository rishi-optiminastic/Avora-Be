"""Absent / On-leave visibility in the attendance range + monthly report.

A working day with no session used to produce no row at all, so the day silently
vanished from the range and monthly views. These prove such days now surface as
`absent`, and as `on_leave` when covered by approved paid leave. Payroll is
unaffected (it counts present = full + half + late only).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.leave import Leave, LeaveStatus, LeaveType
from tests.conftest import _Seed, auth_headers


def _recent_weekday() -> date:
    """This week's Monday — always a past-or-today working day, never a weekend,
    and (in the disposable test DB) never a holiday."""
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def _start_of(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def test_working_day_with_no_session_is_absent(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday().isoformat()
    resp = await client.get(
        f"/api/v1/attendance/range?start={day}&end={day}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    report_rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    assert len(report_rows) == 1
    assert report_rows[0]["status"] == "absent"


async def test_approved_leave_shows_as_on_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday()
    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.PLANNED,
            start_date=_start_of(day),
            end_date=_start_of(day),
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()

    iso = day.isoformat()
    resp = await client.get(
        f"/api/v1/attendance/range?start={iso}&end={iso}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    report_rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    assert len(report_rows) == 1
    assert report_rows[0]["status"] == "on_leave"


async def test_monthly_report_counts_absent_and_leave(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday()
    db.add(
        Leave(
            employee_id=seed.report.id,
            leave_type=LeaveType.SICK,  # a paid type
            start_date=_start_of(day),
            end_date=_start_of(day),
            status=LeaveStatus.APPROVED,
        )
    )
    await db.commit()

    month = day.strftime("%Y-%m")
    resp = await client.get(
        f"/api/v1/attendance/report?month={month}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    report = next(r for r in resp.json() if r["employee_id"] == str(seed.report.id))
    # One paid-leave day this month, and other elapsed working days with no session
    # are absent — both are now visible instead of missing.
    assert report["leave_days"] >= 1
    assert report["absent_days"] >= 1
