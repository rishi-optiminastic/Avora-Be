"""it_admin is treated as a full admin (org decision).

The role is normalised at identity resolution, so every authorization check sees
ADMIN. The stored employee role stays `it_admin` for display; only effective
access changes.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.schemas.auth import CurrentUser
from tests.conftest import _Seed, auth_headers


def test_current_user_normalizes_it_admin_to_admin() -> None:
    u = CurrentUser(employee_id=uuid.uuid4(), role=Role.IT_ADMIN, manager_id=None)
    assert u.role is Role.ADMIN
    assert u.is_admin is True


async def _make_it_admin(db: AsyncSession) -> Employee:
    emp = Employee(
        hr_external_id="it-admin",
        work_email="itadmin@corp.test",
        full_name="Ivy IT",
        role=Role.IT_ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(emp)
    await db.commit()
    return emp


async def test_it_admin_reaches_admin_only_payroll(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    it_admin = await _make_it_admin(db)
    h = auth_headers(settings, it_admin)

    # Payroll is admin/HR-only; it_admin now gets in.
    est = await client.get("/api/v1/payroll/estimate?month=2026-06", headers=h)
    assert est.status_code == 200
    assert (
        await client.get("/api/v1/payroll/adjustments?month=2026-06", headers=h)
    ).status_code == 200

    # /me reports the EFFECTIVE role so the UI shows the admin surface.
    me = await client.get("/api/v1/employees/me", headers=h)
    assert me.status_code == 200
    assert me.json()["role"] == "admin"
