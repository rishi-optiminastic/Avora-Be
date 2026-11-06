"""Days taken before Avora held leave records ("taken before Avora").

HR used to express "all 4 sick days were used in Zoho" by lowering the quota to
0, which every screen then read as "no sick leave". These tests pin the model
that replaced it: the days count as USED, the quota stays what the policy says,
and the figures belong to exactly one leave year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.leave_year import leave_year_window
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.leave_allocation import LeaveAllocation
from tests.conftest import _Seed, auth_headers

_EIGHT_YEARS = timedelta(days=8 * 365)


async def _make_tenured(db: AsyncSession, employee_id: object) -> None:
    """Long-serving, so every bucket is open and the quota is the org policy's."""
    await db.execute(
        update(Employee)
        .where(Employee.id == employee_id)
        .values(hire_date=datetime.now(UTC).date() - _EIGHT_YEARS)
    )
    await db.commit()


async def _sick_row(client: AsyncClient, settings: Settings, actor: object) -> dict[str, object]:
    res = await client.get("/api/v1/leaves/balance", headers=auth_headers(settings, actor))
    assert res.status_code == 200, res.text
    rows = {r["leave_type"]: r for r in res.json()["balances"]}
    row: dict[str, object] = rows[LeaveType.SICK.value]
    return row


async def _enter_sick_taken_before(
    client: AsyncClient, settings: Settings, seed: _Seed, days: float
) -> dict[str, object]:
    res = await client.put(
        f"/api/v1/employees/{seed.report.id}/leave-allocation",
        json={"sick_used_before": days},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 200, res.text
    body: dict[str, object] = res.json()
    return body


async def test_days_taken_before_avora_count_as_used_not_as_a_smaller_quota(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _make_tenured(db, seed.report.id)
    before = await _sick_row(client, settings, seed.report)

    saved = await _enter_sick_taken_before(client, settings, seed, 4)
    this_year = leave_year_window(datetime.now(UTC).date())[0]
    assert saved["used_before_year_start"] == this_year.isoformat()

    after = await _sick_row(client, settings, seed.report)
    assert after["allocated"] == before["allocated"], "the quota itself must not move"
    assert after["used"] == float(before["used"]) + 4  # type: ignore[arg-type]
    assert after["used_before"] == 4.0
    assert after["remaining"] == float(before["remaining"]) - 4  # type: ignore[arg-type]


async def test_taken_before_days_belong_to_one_leave_year_only(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """On 1 April the figure must fall away by itself. Lowering the quota never
    did, which silently pre-spent the fresh year for anyone HR had adjusted."""
    await _make_tenured(db, seed.report.id)
    await _enter_sick_taken_before(client, settings, seed, 4)

    # Re-stamp the row as if HR had saved it during LAST leave year.
    this_year = leave_year_window(datetime.now(UTC).date())[0]
    last_year = date(this_year.year - 1, this_year.month, this_year.day)
    await db.execute(
        update(LeaveAllocation)
        .where(LeaveAllocation.employee_id == seed.report.id)
        .values(used_before_year_start=last_year)
    )
    await db.commit()

    row = await _sick_row(client, settings, seed.report)
    assert row["used_before"] == 0.0
    assert row["used"] == 0.0


async def test_taken_before_days_come_in_halves(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Half a day is the finest grain leave is booked in; a quarter day could
    never be reconciled against anything."""
    headers = auth_headers(settings, seed.admin)
    url = f"/api/v1/employees/{seed.report.id}/leave-allocation"
    quarter = await client.put(url, json={"sick_used_before": 1.25}, headers=headers)
    assert quarter.status_code == 422, quarter.text
    half = await client.put(url, json={"sick_used_before": 1.5}, headers=headers)
    assert half.status_code == 200, half.text
    assert half.json()["sick_used_before"] == 1.5


async def test_a_quota_only_override_carries_no_year(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.put(
        f"/api/v1/employees/{seed.report.id}/leave-allocation",
        json={"sick_days": 6},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 200, res.text
    assert res.json()["used_before_year_start"] is None
