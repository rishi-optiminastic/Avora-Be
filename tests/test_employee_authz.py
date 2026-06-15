"""Authorization tests — every protected endpoint proves out-of-scope = 404/403.

This file is the §9 "every protected endpoint must have an authorization test"
requirement made concrete.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers


async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.get(f"/api/v1/employees/{seed.report.id}")
    assert resp.status_code == 401


async def test_manager_can_read_direct_report(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"/api/v1/employees/{seed.report.id}",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(seed.report.id)


async def test_manager_cannot_read_outsider_gets_404(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Out of scope -> 404 (never 403) so existence is not leaked.
    resp = await client.get(
        f"/api/v1/employees/{seed.outsider.id}",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 404


async def test_employee_cannot_read_peer(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get(
        f"/api/v1/employees/{seed.outsider.id}",
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 404


async def test_list_is_scoped_to_caller(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The manager sees themselves + their report only (2 rows), not the whole org.
    resp = await client.get(
        "/api/v1/employees",
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {str(seed.manager.id), str(seed.report.id)}


async def test_admin_sees_whole_org(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.get(
        "/api/v1/employees",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 4


async def test_non_admin_cannot_change_role(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.put(
        f"/api/v1/employees/{seed.report.id}/role",
        json={"role": "admin"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 403


async def test_admin_can_change_role(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.put(
        f"/api/v1/employees/{seed.report.id}/role",
        json={"role": "manager"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "manager"


async def test_senior_manager_scoped_to_their_department(
    client: AsyncClient, settings: Settings, db: AsyncSession
) -> None:
    # A senior_manager sees their whole department — and nothing outside it.
    sm = Employee(
        hr_external_id="hr-sm",
        work_email="sm@corp.test",
        full_name="Sam Senior",
        role=Role.SENIOR_MANAGER,
        department="Engineering",
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    peer = Employee(
        hr_external_id="hr-eng",
        work_email="eng@corp.test",
        full_name="Eng Peer",
        role=Role.EMPLOYEE,
        department="Engineering",
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    other = Employee(
        hr_external_id="hr-sales",
        work_email="sales@corp.test",
        full_name="Sal Sales",
        role=Role.EMPLOYEE,
        department="Sales",
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add_all([sm, peer, other])
    await db.commit()

    resp = await client.get("/api/v1/employees", headers=auth_headers(settings, sm))
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {str(sm.id), str(peer.id)}  # Engineering only, not Sales


async def test_me_returns_own_record_with_role(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get("/api/v1/employees/me", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(seed.manager.id)
    assert body["role"] == "manager"
