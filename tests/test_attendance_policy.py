"""Office-timings policy + the pure day classifier + regularization workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.attendance import PolicySpec, classify_day
from app.core.config import Settings
from app.schemas.monitoring import AttendanceStatus
from tests.conftest import _Seed, auth_headers

# 09:00 start, 15m buffer (on-time ≤ 09:15), +30m reg window (≤ 09:45),
# full ≥ 480m (15m hours grace ⇒ effective ≥ 465m), half < 240m.
# UTC tz so test times are unambiguous.
_POLICY = PolicySpec(
    work_start_minute=540,
    work_end_minute=1080,
    buffer_minutes=15,
    regularization_window_minutes=30,
    full_day_min_minutes=480,
    half_day_min_minutes=240,
    full_day_grace_minutes=15,
    monthly_regularizations=2,
    working_days_per_week=5,
    timezone="UTC",
)


async def test_working_days_per_week_round_trips(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    read = await client.get("/api/v1/attendance/policy", headers=auth_headers(settings, seed.admin))
    assert read.status_code == 200
    assert read.json()["working_days_per_week"] == 5  # default (Mon-Fri)

    upd = await client.put(
        "/api/v1/attendance/policy",
        json={"working_days_per_week": 6},
        headers=auth_headers(settings, seed.admin),
    )
    assert upd.status_code == 200
    assert upd.json()["working_days_per_week"] == 6

    # A non-manager may read the policy but never change it.
    forbidden = await client.put(
        "/api/v1/attendance/policy",
        json={"working_days_per_week": 7},
        headers=auth_headers(settings, seed.report),
    )
    assert forbidden.status_code == 403


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 1, hour, minute, tzinfo=UTC)


def test_on_time_full_day() -> None:
    v = classify_day(login_at=_at(9, 5), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.late_login is False


def test_a_late_arrival_short_of_the_hours_owed_is_regularizable() -> None:
    """500 worked minutes clears an on-time day but not the 8 hours a late arrival
    owes, so the day is Late and can be contested."""
    v = classify_day(login_at=_at(9, 30), worked_minutes=470, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.LATE
    assert v.regularizable is True


def test_regularized_late_becomes_full() -> None:
    v = classify_day(login_at=_at(9, 30), worked_minutes=500, regularized=True, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.regularized is True


def test_a_very_late_arrival_short_of_the_hours_is_not_a_full_day() -> None:
    """There is no arrival cutoff any more — the hours owed decide. 400 minutes is
    well short of the 8 hours a 10:30 start owes."""
    v = classify_day(login_at=_at(10, 30), worked_minutes=400, regularized=False, policy=_POLICY)
    assert v.status is not AttendanceStatus.FULL_DAY
    assert v.late_login is True


def test_too_few_hours_is_half_day() -> None:
    v = classify_day(login_at=_at(9, 0), worked_minutes=120, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.HALF_DAY
    assert v.early_logout is True


def test_hours_grace_band_near_miss_is_full_day() -> None:
    # On time, worked 465m — 15m short of the 480m bar but inside the grace band.
    # Before the grace band this fell straight to half day (the one-minute cliff).
    v = classify_day(login_at=_at(9, 5), worked_minutes=465, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.early_logout is False


def test_hours_grace_band_just_below_is_early_logout() -> None:
    # 464m is one minute below the graced 465m cutoff — still a full-day miss.
    v = classify_day(login_at=_at(9, 5), worked_minutes=464, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.HALF_DAY
    assert v.early_logout is True


def test_hours_grace_zero_restores_hard_cutoff() -> None:
    # With the grace band off, the pre-fix behaviour holds: 479m ⇒ not a full day.
    strict = replace(_POLICY, full_day_grace_minutes=0)
    v = classify_day(login_at=_at(9, 5), worked_minutes=479, regularized=False, policy=strict)
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


def test_in_progress_late_is_not_judged_before_the_day_ends() -> None:
    """A late arrival can still work the 8 hours owed, so mid-day the verdict is
    Present, not a half day already decided against them."""
    v = classify_day(
        login_at=_at(10, 30),
        worked_minutes=60,
        regularized=False,
        policy=_POLICY,
        day_complete=False,
    )
    assert v.status is AttendanceStatus.PRESENT
    assert v.late_login is True


def test_in_progress_late_shows_as_present_with_the_late_flag() -> None:
    """The lateness is recorded immediately (it counts toward the monthly
    allowance), but the day is not scored until the hours are final."""
    v = classify_day(
        login_at=_at(9, 30),
        worked_minutes=60,
        regularized=False,
        policy=_POLICY,
        day_complete=False,
    )
    assert v.status is AttendanceStatus.PRESENT
    assert v.late_login is True


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


def test_arriving_late_and_falling_short_can_still_be_contested() -> None:
    """The narrow window was the bug: someone an hour late had no way to ask.

    Arriving late is exactly when a person has a story worth telling (traffic, a
    client visit, a doctor). The monthly credit cap — enforced when a request is
    approved — is what limits this, not eligibility.
    """
    # Late, and short of the 8 hours a late arrival owes.
    v = classify_day(login_at=_at(10, 30), worked_minutes=400, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.LATE
    assert v.regularizable is True


def test_a_late_arrival_who_works_the_full_eight_hours_keeps_the_day() -> None:
    """Attendance Guidelines 2026: lateness is paid for by owing 8 working hours
    from the actual arrival, and by the 3-a-month allowance — not by an arrival
    cutoff that made a 10:30 start unsalvageable however long the person stayed.
    """
    v = classify_day(login_at=_at(10, 30), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.late_login is True  # still counts toward the monthly allowance


def test_a_day_with_no_punch_can_be_contested() -> None:
    """A missed biometric scan is the single most common reason to regularize, and
    it used to be the one case the option was hidden for."""
    v = classify_day(login_at=None, worked_minutes=0, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.ABSENT
    assert v.regularizable is True


def test_short_hours_can_be_contested() -> None:
    v = classify_day(
        login_at=_at(9, 5), worked_minutes=100, regularized=False, day_complete=True, policy=_POLICY
    )
    assert v.status is AttendanceStatus.HALF_DAY
    assert v.regularizable is True


def test_a_clean_full_day_has_nothing_to_contest() -> None:
    v = classify_day(login_at=_at(9, 5), worked_minutes=500, regularized=False, policy=_POLICY)
    assert v.status is AttendanceStatus.FULL_DAY
    assert v.regularizable is False


def test_an_already_regularized_day_is_not_offered_again() -> None:
    v = classify_day(login_at=None, worked_minutes=0, regularized=True, policy=_POLICY)
    assert v.regularizable is False
