"""The monthly attendance register (.xlsx).

Two things matter beyond "does a file come back". The sheet must agree with the
API report - they read the same rollup, so a month can never be counted twice
two ways - and the export must not widen scope: an export that shows more than
the screen is a data leak with a filename (CLAUDE.md §9, security rule 5.3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.work_session import WorkSession
from tests.conftest import _Seed, auth_headers

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _recent_weekday() -> date:
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


async def _worked(db: AsyncSession, employee_id: object, day: date) -> None:
    """A 9:30-to-18:00 day, in UTC (the test org runs on UTC)."""
    db.add(
        WorkSession(
            employee_id=employee_id,
            clock_in_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=UTC),
            clock_out_at=datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC),
            source="dashboard",
        )
    )
    await db.commit()


async def _export(client: AsyncClient, settings: Settings, who: object, month: str):
    return await client.get(
        f"/api/v1/attendance/report/export?month={month}",
        headers=auth_headers(settings, who),  # type: ignore[arg-type]
    )


def _sheets(content: bytes):
    return load_workbook(BytesIO(content))


async def test_export_returns_a_two_sheet_workbook(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    day = _recent_weekday()
    await _worked(db, seed.report.id, day)

    res = await _export(client, settings, seed.admin, _month_of(day))

    assert res.status_code == 200
    assert res.headers["content-type"].startswith(_XLSX)
    assert f"attendance-{_month_of(day)}.xlsx" in res.headers["content-disposition"]
    wb = _sheets(res.content)
    assert len(wb.sheetnames) == 2
    assert wb.sheetnames[0].startswith("Summary")
    assert wb.sheetnames[1].startswith("Daily")


async def test_daily_sheet_carries_check_in_and_check_out(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The whole point of the report: the actual in/out times, not just totals."""
    day = _recent_weekday()
    await _worked(db, seed.report.id, day)

    res = await _export(client, settings, seed.admin, _month_of(day))

    daily = _sheets(res.content)[_sheets(res.content).sheetnames[1]]
    headers = [c.value for c in daily[1]]
    assert "Check in" in headers and "Check out" in headers
    rows = [
        dict(zip(headers, [c.value for c in row], strict=True))
        for row in daily.iter_rows(min_row=2)
    ]
    mine = [
        r for r in rows if r["Employee"] == seed.report.full_name and r["Date"] == day.isoformat()
    ]
    assert len(mine) == 1
    assert mine[0]["Check in"] == "09:30"
    assert mine[0]["Check out"] == "18:00"
    assert mine[0]["Hours"] == 8.5


async def test_summary_sheet_agrees_with_the_api_report(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Both read the same rollup, so the sheet can never contradict the screen."""
    day = _recent_weekday()
    await _worked(db, seed.report.id, day)
    month = _month_of(day)

    api = await client.get(
        f"/api/v1/attendance/report?month={month}", headers=auth_headers(settings, seed.admin)
    )
    res = await _export(client, settings, seed.admin, month)

    assert api.status_code == 200
    by_id = {r["employee_id"]: r for r in api.json()}
    wb = _sheets(res.content)
    summary = wb[wb.sheetnames[0]]
    headers = [c.value for c in summary[1]]
    rows = {
        r[headers.index("Employee")]: r
        for r in ([c.value for c in row] for row in summary.iter_rows(min_row=2))
    }
    mine = rows[seed.report.full_name]
    expected = by_id[str(seed.report.id)]
    assert mine[headers.index("Full days")] == expected["full_days"]
    assert mine[headers.index("Absent days")] == expected["absent_days"]
    assert mine[headers.index("Total hours")] == round(expected["worked_minutes"] / 60, 2)


async def test_export_is_scoped_like_every_other_attendance_read(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """An employee's export contains only themselves. Exporting must never be a
    way to see people the screen would not show."""
    day = _recent_weekday()
    await _worked(db, seed.report.id, day)
    await _worked(db, seed.outsider.id, day)

    res = await _export(client, settings, seed.outsider, _month_of(day))

    assert res.status_code == 200
    wb = _sheets(res.content)
    summary = wb[wb.sheetnames[0]]
    names = {row[0].value for row in summary.iter_rows(min_row=2)}
    assert names == {seed.outsider.full_name}
    assert seed.report.full_name not in names


async def test_a_bad_month_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await _export(client, settings, seed.admin, "not-a-month")

    assert res.status_code == 422


async def test_unauthenticated_is_rejected(client: AsyncClient) -> None:
    res = await client.get("/api/v1/attendance/report/export?month=2026-09")

    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# Typical check-in / check-out. The monthly report answered "how many days" but
# never "when did they actually arrive", so the only way to see a time was to
# open the spreadsheet. These pin the month-level figure that fills that gap.
#
# Times here are OFFICE-local. The policy timezone is Asia/Kolkata, and sessions
# are stored as UTC instants, so a test that writes a bare UTC hour and expects
# to read it back unchanged is only ever right by accident on an IST laptop.
# --------------------------------------------------------------------------- #

_OFFICE = ZoneInfo("Asia/Kolkata")


def _at(day: date, hour: int, minute: int) -> datetime:
    """An office wall-clock time, as the UTC instant actually stored."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_OFFICE).astimezone(UTC)


async def _office_day(
    db: AsyncSession,
    employee_id: object,
    day: date,
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    db.add(
        WorkSession(
            employee_id=employee_id,
            clock_in_at=_at(day, *start),
            clock_out_at=_at(day, *end),
            source="dashboard",
        )
    )
    await db.commit()


async def _summary_for(
    client: AsyncClient, settings: Settings, who: object, month: str, employee_id: object
) -> dict:
    res = await client.get(
        f"/api/v1/attendance/report?month={month}",
        headers=auth_headers(settings, who),  # type: ignore[arg-type]
    )
    assert res.status_code == 200, res.text
    rows = [r for r in res.json() if r["employee_id"] == str(employee_id)]
    assert len(rows) == 1
    return rows[0]


async def test_report_averages_check_in_and_check_out(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    monday = _recent_weekday()
    await _office_day(db, seed.report.id, monday, (9, 0), (17, 0))
    await _office_day(db, seed.report.id, monday + timedelta(days=1), (10, 0), (19, 0))

    row = await _summary_for(client, settings, seed.admin, _month_of(monday), seed.report.id)

    assert row["avg_check_in_minutes"] == 9 * 60 + 30  # 09:30 office time
    assert row["avg_check_out_minutes"] == 18 * 60  # 18:00 office time


async def test_absent_days_do_not_drag_the_average_toward_midnight(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The averages cover only the days that had a punch.

    Folding an absent day in as 00:00 would make one missed day look like the
    person arrived in the middle of the night - the kind of number that ends up
    in a performance conversation.
    """
    monday = _recent_weekday()
    await _office_day(db, seed.report.id, monday, (9, 0), (18, 0))

    row = await _summary_for(client, settings, seed.admin, _month_of(monday), seed.report.id)

    assert row["absent_days"] > 0, "precondition: the month has days with no session"
    assert row["avg_check_in_minutes"] == 9 * 60
    assert row["avg_check_out_minutes"] == 18 * 60


async def test_never_checked_in_reads_as_unknown_not_midnight(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    monday = _recent_weekday()
    await _office_day(db, seed.report.id, monday, (9, 0), (18, 0))

    row = await _summary_for(client, settings, seed.admin, _month_of(monday), seed.manager.id)

    assert row["avg_check_in_minutes"] is None
    assert row["avg_check_out_minutes"] is None


async def test_summary_sheet_carries_the_average_times(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    monday = _recent_weekday()
    await _office_day(db, seed.report.id, monday, (9, 0), (17, 0))
    await _office_day(db, seed.report.id, monday + timedelta(days=1), (10, 0), (19, 0))

    res = await _export(client, settings, seed.admin, _month_of(monday))

    wb = _sheets(res.content)
    sheet = wb[wb.sheetnames[0]]
    headers = [c.value for c in sheet[1]]
    assert "Avg check in" in headers and "Avg check out" in headers
    rows = [
        dict(zip(headers, [c.value for c in row], strict=True))
        for row in sheet.iter_rows(min_row=2)
    ]
    mine = [r for r in rows if r["Employee"] == seed.report.full_name]
    assert len(mine) == 1
    assert mine[0]["Avg check in"] == "09:30"
    assert mine[0]["Avg check out"] == "18:00"
    # Somebody with no session all month reads as "-", never "00:00".
    others = [r for r in rows if r["Employee"] != seed.report.full_name]
    assert all(r["Avg check in"] == "-" for r in others)
