"""Leave balance & policy — the self-service entitlement view.

Covers the pure day-counting / leave-year helpers, the balance math (allocated
minus used minus pending, weekends + holidays excluded, half-day = 0.5), the joining-
anniversary window, scope (you can read your own and your reports', not an
outsider's), and that only HR/Admin may change the org leave policy (CLAUDE §9).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.payroll import working_days_between
from app.models.holiday import Holiday, HolidayType
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.services.leave_service import _leave_year_window
from tests.conftest import _Seed, auth_headers

# ---- pure helpers ---------------------------------------------------------- #


def test_working_days_between_excludes_weekends() -> None:
    # Fri 2026-06-19 .. Mon 2026-06-22 -> Fri + Mon = 2 working days.
    assert working_days_between(date(2026, 6, 19), date(2026, 6, 22), set()) == 2


def test_working_days_between_excludes_holidays() -> None:
    # Mon..Fri = 5; drop the Wed holiday -> 4.
    assert working_days_between(date(2026, 6, 15), date(2026, 6, 19), set()) == 5
    assert working_days_between(date(2026, 6, 15), date(2026, 6, 19), {date(2026, 6, 17)}) == 4


def test_working_days_between_inverted_range_is_zero() -> None:
    assert working_days_between(date(2026, 6, 20), date(2026, 6, 15), set()) == 0


def test_leave_year_window_after_anniversary() -> None:
    start, end = _leave_year_window(date(2020, 3, 10), date(2026, 6, 22))
    assert start == date(2026, 3, 10)
    assert end == date(2027, 3, 9)


def test_leave_year_window_before_anniversary() -> None:
    start, end = _leave_year_window(date(2020, 9, 10), date(2026, 6, 22))
    assert start == date(2025, 9, 10)
    assert end == date(2026, 9, 9)


def test_leave_year_window_leap_day_anchor() -> None:
    start, end = _leave_year_window(date(2020, 2, 29), date(2025, 6, 1))
    assert start == date(2025, 2, 28)
    assert end == date(2026, 2, 27)


# ---- balance over real leaves ---------------------------------------------- #


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


async def _seed_leaves(db: AsyncSession, seed: _Seed) -> dict[str, float]:
    """Give the report a fixed hire date and a few leaves; return expected counts."""
    now = datetime.now(UTC)
    # ~13 months back, for two reasons: the current leave-year window still covers
    # `now` (the anniversary was ~5 weeks ago, before this week's leaves), AND the
    # employee lands in the TENURED band so these tests exercise the org policy
    # defaults rather than a tenure-band quota. Band-specific entitlement is
    # covered separately in test_leave_tenure.py.
    seed.report.hire_date = (now - timedelta(days=400)).date()
    monday = now.date() - timedelta(days=now.weekday())  # this week's Monday
    planned_start, planned_end = monday, monday + timedelta(days=2)  # Mon..Wed
    holiday_date = monday + timedelta(days=1)  # Tue — excluded from the count
    sick_day = monday + timedelta(days=3)  # Thu

    db.add_all(
        [
            Leave(
                employee_id=seed.report.id,
                leave_type=LeaveType.PLANNED,
                start_date=_dt(planned_start),
                end_date=_dt(planned_end),
                status=LeaveStatus.APPROVED,
            ),
            Leave(
                employee_id=seed.report.id,
                leave_type=LeaveType.SICK,
                start_date=_dt(sick_day),
                end_date=_dt(sick_day),
                status=LeaveStatus.SUBMITTED,
            ),
            Leave(
                employee_id=seed.report.id,
                leave_type=LeaveType.UNPAID,
                start_date=_dt(monday + timedelta(days=4)),  # Fri
                end_date=_dt(monday + timedelta(days=4)),
                status=LeaveStatus.APPROVED,
            ),
            Holiday(name="Midweek", date=holiday_date, holiday_type=HolidayType.PUBLIC),
        ]
    )
    await db.commit()
    return {
        "planned_used": float(working_days_between(planned_start, planned_end, {holiday_date})),
        "sick_pending": float(working_days_between(sick_day, sick_day, set())),
    }


async def test_balance_self(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    expected = await _seed_leaves(db, seed)

    resp = await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 200
    body = resp.json()
    by_type = {b["leave_type"]: b for b in body["balances"]}

    planned = by_type["planned"]
    assert planned["allocated"] == 8  # written policy: 8 planned days
    assert planned["used"] == expected["planned_used"]
    assert planned["pending"] == 0
    assert planned["remaining"] == 8 - expected["planned_used"]

    sick = by_type["sick"]
    assert sick["allocated"] == 6
    assert sick["used"] == 0
    assert sick["pending"] == expected["sick_pending"]
    assert sick["remaining"] == 6 - expected["sick_pending"]


async def test_balance_scope(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _seed_leaves(db, seed)
    rid = str(seed.report.id)

    # The report's own manager and an admin may read it.
    for actor in (seed.manager, seed.admin):
        resp = await client.get(
            f"/api/v1/leaves/balance?employee_id={rid}", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 200, f"{actor.work_email} should read report balance"

    # An unrelated employee may not — 404 (we don't even reveal existence, §7).
    outsider = await client.get(
        f"/api/v1/leaves/balance?employee_id={rid}", headers=auth_headers(settings, seed.outsider)
    )
    assert outsider.status_code == 404


# ---- leave policy ---------------------------------------------------------- #


async def test_policy_readable_by_everyone(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get("/api/v1/leaves/policy", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 200
    assert resp.json()["annual_planned_days"] == 8


async def test_policy_update_is_hr_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager):
        resp = await client.put(
            "/api/v1/leaves/policy",
            json={"annual_planned_days": 20},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.work_email} must not edit the leave policy"

    ok = await client.put(
        "/api/v1/leaves/policy",
        json={"annual_planned_days": 20},
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 200
    assert ok.json()["annual_planned_days"] == 20


async def test_policy_change_reflects_in_balance(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    expected = await _seed_leaves(db, seed)
    await client.put(
        "/api/v1/leaves/policy",
        json={"annual_planned_days": 20},
        headers=auth_headers(settings, seed.admin),
    )
    body = (
        await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, seed.report))
    ).json()
    planned = next(b for b in body["balances"] if b["leave_type"] == "planned")
    assert planned["allocated"] == 20
    assert planned["remaining"] == 20 - expected["planned_used"]
