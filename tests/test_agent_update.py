"""On-demand agent self-update broadcast (POST /devices/update-all).

Authorization is the point (CLAUDE.md §9): only admin / IT-admin (the
device-fleet roles) may push an update — everyone else gets 403.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/devices/update-all"


async def _add(db: AsyncSession, role: Role, email: str) -> Employee:
    emp = Employee(
        hr_external_id=f"x-{email}",
        work_email=email,
        full_name="Test",
        role=role,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(emp)
    await db.commit()
    return emp


async def test_only_admin_and_it_admin_can_broadcast_update(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    # Manager / employee can manage their own people but NOT the device fleet.
    assert (await client.post(URL, headers=auth_headers(settings, seed.manager))).status_code == 403
    assert (await client.post(URL, headers=auth_headers(settings, seed.report))).status_code == 403

    # Admin can.
    admin_resp = await client.post(URL, headers=auth_headers(settings, seed.admin))
    assert admin_resp.status_code == 200
    assert "updated" in admin_resp.json()

    # IT-admin (device + system health role) can too.
    it_admin = await _add(db, Role.IT_ADMIN, "it@corp.test")
    assert (await client.post(URL, headers=auth_headers(settings, it_admin))).status_code == 200
