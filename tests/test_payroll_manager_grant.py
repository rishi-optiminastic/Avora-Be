"""The `payroll_manager` capability grant.

A non-HR person (e.g. a finance executive) can be granted management of the
payroll cluster — payroll, payroll adjustments, attendance adjustments, and
compensation — without becoming HR/Admin. Only an admin may set the grant.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.work_session import WorkSession
from tests.conftest import _Seed, auth_headers

_MONTH = "2026-06"


def _grant(employee_id: str, on: bool) -> tuple[str, dict[str, object]]:
    return f"/api/v1/employees/{employee_id}/payroll-manager", {"payroll_manager": on}


async def test_only_admin_sets_the_grant(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    url, body = _grant(str(seed.outsider.id), True)
    for actor in (seed.manager, seed.report, seed.outsider):
        denied = await client.put(url, json=body, headers=auth_headers(settings, actor))
        assert denied.status_code == 403

    ok = await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    assert ok.status_code == 200, ok.text
    assert ok.json()["payroll_manager"] is True


async def test_grant_unlocks_payroll_cluster(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    exec_headers = auth_headers(settings, seed.outsider)
    # Before the grant, a plain employee is forbidden across the cluster.
    assert (await client.get("/api/v1/payroll/settings", headers=exec_headers)).status_code == 403
    assert (
        await client.get(f"/api/v1/payroll/adjustments?month={_MONTH}", headers=exec_headers)
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/attendance/overrides?month={_MONTH}", headers=exec_headers)
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/employees/{seed.report.id}/compensation", headers=exec_headers)
    ).status_code == 403

    url, body = _grant(str(seed.outsider.id), True)
    granted = await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    assert granted.status_code == 200

    # After the grant, the cluster opens (200, or 404 for a missing comp row — not 403).
    assert (await client.get("/api/v1/payroll/settings", headers=exec_headers)).status_code == 200
    assert (
        await client.get(f"/api/v1/payroll/adjustments?month={_MONTH}", headers=exec_headers)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/attendance/overrides?month={_MONTH}", headers=exec_headers)
    ).status_code == 200
    comp = await client.get(f"/api/v1/compensation/{seed.report.id}", headers=exec_headers)
    assert comp.status_code in (200, 404)  # authorized now; 404 only if no comp on file

    # Revoking closes it again.
    url, body = _grant(str(seed.outsider.id), False)
    await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    assert (await client.get("/api/v1/payroll/settings", headers=exec_headers)).status_code == 403


async def test_grant_holder_runs_payroll_for_the_whole_org(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A finance person with the grant has the EMPLOYEE role, whose viewing scope
    is just themselves. Payroll must still cover everyone, or the estimate, the
    exports and the payout check would silently leave the org out."""
    url, body = _grant(str(seed.outsider.id), True)
    await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    as_finance = await client.get(
        f"/api/v1/payroll/estimate?month={_MONTH}", headers=auth_headers(settings, seed.outsider)
    )
    as_admin = await client.get(
        f"/api/v1/payroll/estimate?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    assert as_finance.status_code == 200
    people = {line["employee_id"] for line in as_finance.json()["lines"]}
    assert {str(seed.report.id), str(seed.manager.id), str(seed.admin.id)} <= people
    assert as_finance.json()["lines"] == as_admin.json()["lines"]


async def test_grant_holder_sees_the_same_slip_as_an_admin(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Their own scope would leave the employee's attendance out and price the
    whole month as loss of pay; the slip must match what an admin sees."""
    # A worked day in the month, so "present" is not zero for everyone anyway.
    db.add(
        WorkSession(
            employee_id=seed.report.id,
            clock_in_at=datetime(2026, 6, 2, 4, 0, tzinfo=UTC),
            clock_out_at=datetime(2026, 6, 2, 13, 0, tzinfo=UTC),
            source="dashboard",
        )
    )
    await db.commit()
    url, body = _grant(str(seed.outsider.id), True)
    await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    path = f"/api/v1/payroll/me?employee_id={seed.report.id}&month={_MONTH}"
    as_finance = await client.get(path, headers=auth_headers(settings, seed.outsider))
    as_admin = await client.get(path, headers=auth_headers(settings, seed.admin))
    assert as_admin.status_code == 200, as_admin.text
    assert as_admin.json()["present_days"] > 0
    assert as_finance.status_code == 200, as_finance.text
    assert as_finance.json()["present_days"] == as_admin.json()["present_days"]
    assert as_finance.json()["lop_days"] == as_admin.json()["lop_days"]
