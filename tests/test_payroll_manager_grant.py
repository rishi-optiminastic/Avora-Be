"""The `payroll_manager` capability grant.

A non-HR person (e.g. a finance executive) can be granted management of the
payroll cluster — payroll, payroll adjustments, attendance adjustments, and
compensation — without becoming HR/Admin. Only an admin may set the grant.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
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
        await client.get(
            f"/api/v1/attendance/overrides?month={_MONTH}", headers=exec_headers
        )
    ).status_code == 403
    assert (
        await client.get(
            f"/api/v1/employees/{seed.report.id}/compensation", headers=exec_headers
        )
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
        await client.get(
            f"/api/v1/attendance/overrides?month={_MONTH}", headers=exec_headers
        )
    ).status_code == 200
    comp = await client.get(f"/api/v1/compensation/{seed.report.id}", headers=exec_headers)
    assert comp.status_code in (200, 404)  # authorized now; 404 only if no comp on file

    # Revoking closes it again.
    url, body = _grant(str(seed.outsider.id), False)
    await client.put(url, json=body, headers=auth_headers(settings, seed.admin))
    assert (await client.get("/api/v1/payroll/settings", headers=exec_headers)).status_code == 403
