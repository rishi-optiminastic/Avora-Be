"""Office-timings policy + the pure day classifier + regularization workflow."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.attendance import PolicySpec, classify_day
from app.core.config import Settings
from app.schemas.monitoring import AttendanceStatus
from tests.conftest import _Seed, auth_headers

# 09:00 start, 15m buffer (on-time ≤ 09:15), +30m reg window (≤ 09:45),
# full ≥ 480m, half < 240m. UTC tz so test times are unambiguous.
_POLICY = PolicySpec(
    work_start_minute=540,
    work_end_minute=1080,
    buffer_minutes=15,
    regularization_window_minutes=30,
    full_day_min_minutes=480,
    half_day_min_minutes=240,
    monthly_regularizations=2,
    timezone="UTC",
)


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 1, hour, minute, tzinfo=UTC)


def test_on_time_full_day() -> None:
    v = classify_day(login_at=_at(9, 5), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.late_login is False


def test_late_in_window_is_regularizable() -> None:
    v = classify_day(login_at=_at(9, 30), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.LATE
    assert v.regularizable is True


def test_regularized_late_becomes_full() -> None:
    v = classify_day(login_at=_at(9, 30), worked_minutes=500, regularized=True, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.regularized is True


def test_too_late_is_half_day() -> None:
    v = classify_day(login_at=_at(10, 30), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.HALF_DAY


def test_too_few_hours_is_half_day() -> None:
    v = classify_day(login_at=_at(9, 0), worked_minutes=120, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.HALF_DAY
    assert v.early_logout is True


def test_in_progress_on_time_is_present_not_half() -> None:
    # Mid-day: on time but only a couple of hours logged so far. Must NOT be
    # downgraded to half day, and the early-out flag must stay off.
    v = classify_day(
        login_at=_at(9, 5),
        worked_minutes=120,
        regularized=False,
        policy=_POLICY,
        day_complete=False,
    )
    assert v.status is AttendanceStatus.PRESENT
    assert v.early_logout is False
    assert v.late_login is False


def test_in_progress_too_late_is_still_half() -> None:
    # Arriving past the reg window is an arrival fact — half day even mid-day.
    v = classify_day(
        login_at=_at(10, 30),
        worked_minutes=60,
        regularized=False,
        policy=_POLICY,
        day_complete=False,
    )
    assert v.status is AttendanceStatus.HALF_DAY


def test_in_progress_late_in_window_is_regularizable() -> None:
    # Late but inside the window, day still running → Late + regularizable.
    v = classify_day(
        login_at=_at(9, 30),
        worked_minutes=60,
        regularized=False,
        policy=_POLICY,
        day_complete=False,
    )
    assert v.status is AttendanceStatus.LATE
    assert v.regularizable is True


def test_no_login_is_absent() -> None:
    v = classify_day(login_at=None, worked_minutes=0, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.ABSENT


# --- policy endpoint authz ------------------------------------------------- #
async def test_policy_read_open_write_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Anyone authenticated can read the policy…
    assert (
        await client.get("/api/v1/attendance/policy", headers=auth_headers(settings, seed.report))
    ).status_code == 200
    # …but only admin/HR may change it.
    body = {"work_start": "10:00", "monthly_regularizations": 3}
    assert (
        await client.put(
            "/api/v1/attendance/policy", json=body, headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    ok = await client.put(
        "/api/v1/attendance/policy", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 200
    assert ok.json()["work_start"] == "10:00"
    assert ok.json()["monthly_regularizations"] == 3
