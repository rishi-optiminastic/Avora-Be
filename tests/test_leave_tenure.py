"""Tenure-banded leave entitlement.

Three bands derived from the joining date — probation (first 6 months), confirmed
(probation done, under a year), tenured (a year+). The band selects the quota, so
these tests pin BOTH the pure date arithmetic and the balance it produces.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenure import (
    TenureStatus,
    accrued_units,
    add_months,
    months_completed,
    probation_end,
    tenure_status,
)
from app.models.leave import LeaveType
from tests.conftest import _Seed, auth_headers

_PROBATION_MONTHS = 6


# --- pure date arithmetic --------------------------------------------------- #
def test_month_addition_clamps_to_a_shorter_month() -> None:
    # Aug 31 + 6 months is the end of February, not an invalid Feb 31.
    assert add_months(date(2025, 8, 31), 6) == date(2026, 2, 28)
    assert add_months(date(2024, 8, 31), 6) == date(2025, 2, 28)
    assert add_months(date(2026, 1, 15), 6) == date(2026, 7, 15)


def test_months_completed_needs_the_day_of_month_anniversary() -> None:
    start = date(2026, 1, 15)
    assert months_completed(start, date(2026, 2, 14)) == 0  # one day short
    assert months_completed(start, date(2026, 2, 15)) == 1  # exactly one month
    assert months_completed(start, date(2027, 1, 15)) == 12


def test_months_completed_never_goes_negative_for_a_future_hire() -> None:
    assert months_completed(date(2026, 12, 1), date(2026, 1, 1)) == 0


def test_the_three_bands_split_on_probation_end_and_one_year() -> None:
    hire = date(2026, 1, 15)
    band = lambda day: tenure_status(hire, day, probation_months=_PROBATION_MONTHS)  # noqa: E731

    assert band(date(2026, 1, 15)) is TenureStatus.PROBATION  # day one
    assert band(date(2026, 7, 14)) is TenureStatus.PROBATION  # one day short of 6m
    assert band(date(2026, 7, 15)) is TenureStatus.CONFIRMED  # probation complete
    assert band(date(2027, 1, 14)) is TenureStatus.CONFIRMED  # one day short of a year
    assert band(date(2027, 1, 15)) is TenureStatus.TENURED  # a year in


def test_probation_end_is_the_first_confirmed_day() -> None:
    assert probation_end(date(2026, 1, 15), _PROBATION_MONTHS) == date(2026, 7, 15)


def test_accrual_credits_up_front_and_carries_forward() -> None:
    start = date(2026, 7, 15)
    # Credited the day eligibility begins — you don't wait a month for the first.
    assert accrued_units(start, date(2026, 7, 15), per_month=1.0) == 1.0
    assert accrued_units(start, date(2026, 8, 14), per_month=1.0) == 1.0
    # Each further month adds one, and unused days simply stay in the total —
    # that IS the carryforward.
    assert accrued_units(start, date(2026, 8, 15), per_month=1.0) == 2.0
    assert accrued_units(start, date(2026, 10, 15), per_month=1.0) == 4.0


def test_accrual_is_capped_at_a_years_worth() -> None:
    start = date(2026, 1, 1)
    assert accrued_units(start, date(2030, 1, 1), per_month=1.0) == 12.0


def test_accrual_is_zero_before_eligibility_starts() -> None:
    assert accrued_units(date(2026, 7, 15), date(2026, 1, 1), per_month=1.0) == 0.0


# --- the balance the API actually returns ----------------------------------- #
async def _balance(client: AsyncClient, settings: Settings, actor: object) -> dict[str, object]:
    resp = await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, actor))
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _allocated(payload: dict[str, object], leave_type: LeaveType) -> float:
    balances = payload["balances"]
    assert isinstance(balances, list)
    for row in balances:
        if row["leave_type"] == leave_type.value:
            return float(row["allocated"])
    raise AssertionError(f"{leave_type} missing from the balance")


async def _set_hire_date(db: AsyncSession, employee_id: object, hire: date) -> None:
    from app.models.employee import Employee

    person = await db.get(Employee, employee_id)
    assert person is not None
    person.hire_date = hire
    await db.commit()


async def test_probation_gets_four_sick_one_birthday_and_no_planned(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _set_hire_date(db, seed.report.id, datetime.now(UTC).date() - timedelta(days=30))

    payload = await _balance(client, settings, seed.report)

    assert payload["tenure_status"] == TenureStatus.PROBATION.value
    assert _allocated(payload, LeaveType.SICK) == 4.0
    assert _allocated(payload, LeaveType.BIRTHDAY) == 1.0
    # Planned/annual time off isn't earned yet.
    assert _allocated(payload, LeaveType.PLANNED) == 0.0
    assert _allocated(payload, LeaveType.ANNUAL) == 0.0


async def test_confirmed_stacks_sick_to_six_and_starts_accruing_planned(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    # Joined 7 months ago: probation done last month, so one accrual month has
    # completed on top of the up-front credit.
    hire = add_months(datetime.now(UTC).date(), -7)
    await _set_hire_date(db, seed.report.id, hire)

    payload = await _balance(client, settings, seed.report)

    assert payload["tenure_status"] == TenureStatus.CONFIRMED.value
    # The probation 4 plus 2 more, counted against the same leave year — days
    # already taken during probation still count against this.
    assert _allocated(payload, LeaveType.SICK) == 6.0
    assert _allocated(payload, LeaveType.PLANNED) == 2.0  # credited on confirmation, +1 a month
    assert LeaveType.PLANNED.value in payload["accruing_types"]  # type: ignore[operator]
    assert payload["probation_end_date"] == add_months(hire, _PROBATION_MONTHS).isoformat()


async def test_tenured_falls_through_to_the_org_policy(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The tenured band has no rules yet, so it must inherit the org defaults
    rather than silently inheriting the confirmed band's."""
    await _set_hire_date(db, seed.report.id, add_months(datetime.now(UTC).date(), -18))

    payload = await _balance(client, settings, seed.report)

    assert payload["tenure_status"] == TenureStatus.TENURED.value
    assert _allocated(payload, LeaveType.PLANNED) == 12.0  # LeavePolicy default
    assert _allocated(payload, LeaveType.SICK) == 8.0
    assert payload["accruing_types"] == []


async def test_a_per_employee_override_still_beats_the_band(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Resolution order is override → band → policy. HR must be able to grant an
    exception to someone on probation."""
    await _set_hire_date(db, seed.report.id, datetime.now(UTC).date() - timedelta(days=30))

    granted = await client.put(
        f"/api/v1/employees/{seed.report.id}/leave-allocation",
        json={"sick_days": 20},
        headers=auth_headers(settings, seed.admin),
    )
    assert granted.status_code in (200, 201), granted.text

    payload = await _balance(client, settings, seed.report)
    assert payload["tenure_status"] == TenureStatus.PROBATION.value
    assert _allocated(payload, LeaveType.SICK) == 20.0  # override wins
    assert _allocated(payload, LeaveType.BIRTHDAY) == 1.0  # band still applies elsewhere


async def test_a_personal_probation_length_overrides_the_org_default(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Probation is negotiated per offer, so a person can carry their own length.

    Joined 4 months ago: still on probation under the org's 6-month default, but
    confirmed under a negotiated 3-month one.
    """
    hire = add_months(datetime.now(UTC).date(), -4)
    await _set_hire_date(db, seed.report.id, hire)

    on_default = await _balance(client, settings, seed.report)
    assert on_default["tenure_status"] == TenureStatus.PROBATION.value

    granted = await client.patch(
        f"/api/v1/employees/{seed.report.id}",
        json={"full_name": seed.report.full_name, "probation_months": 3},
        headers=auth_headers(settings, seed.admin),
    )
    assert granted.status_code == 200, granted.text

    overridden = await _balance(client, settings, seed.report)
    assert overridden["tenure_status"] == TenureStatus.CONFIRMED.value
    # The confirmation date must move with it, or the badge and the entitlement
    # would tell the employee two different stories.
    assert overridden["probation_end_date"] == add_months(hire, 3).isoformat()
