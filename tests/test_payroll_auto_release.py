"""Auto-release of the previous month's payslips on the configured day.

The manual "Release" click stays primary; this is the hands-off fallback the
scheduler runs on `auto_release_day`. `release_for_system` releases the PREVIOUS
month (salary is processed after month-end), is idempotent, and never re-emails a
month that was already released.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_override import AttendanceOverrideRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.org_settings import OrgSettingsRepository
from app.repositories.payroll_adjustment import PayrollAdjustmentRepository
from app.repositories.payroll_run import PayrollRunRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.payslip import PayslipRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.reimbursement import ReimbursementRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.payroll_service import PayrollService
from tests.conftest import _FakeEmailService, _Seed, auth_headers

# Today is 2026-07-31 in the suite -> the previous month is June.
_PREV = "2026-06"
_COMP = {"amount_minor": 50_000_00, "currency": "inr", "period": "monthly"}
_SETTINGS = {
    "pay_day_of_month": 1,
    "currency": "INR",
    "pay_cycle": "monthly",
    "auto_send_enabled": False,
    "auto_release_enabled": False,
    "auto_release_day": 8,
    "recipients": [],
    "basic_pct": 30,
    "hra_pct": 50,
    "pf_pct": 12,
    "pf_cap_minor": 1_800_00,
    "professional_tax_minor": 200_00,
    "professional_tax_feb_minor": 300_00,
    "deduct_income_tax": True,
}


def _service(db: AsyncSession, settings: Settings) -> PayrollService:
    audit = AuditRepository(db)
    employees = EmployeeRepository(db)
    policy = AttendancePolicyService(AttendancePolicyRepository(db), audit)
    attendance = AttendanceService(
        employees,
        ActivityRepository(db),
        WorkSessionRepository(db),
        policy,
        RegularizationRepository(db),
        AttendanceOverrideRepository(db),
        HolidayRepository(db),
        LeaveRepository(db),
    )
    return PayrollService(
        PayrollSettingsRepository(db),
        PayrollRunRepository(db),
        PayslipRepository(db),
        CompensationRepository(db),
        employees,
        attendance,
        policy,
        LeaveRepository(db),
        HolidayRepository(db),
        OrgSettingsRepository(db),
        ReimbursementRepository(db),
        PayrollAdjustmentRepository(db),
        _FakeEmailService(),  # type: ignore[arg-type]
        audit,
        settings,
    )


async def _setup(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    await client.put(
        "/api/v1/payroll/settings", json=_SETTINGS, headers=auth_headers(settings, seed.admin)
    )


async def test_settings_round_trip_persists_auto_release(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {**_SETTINGS, "auto_release_enabled": True, "auto_release_day": 8}
    resp = await client.put(
        "/api/v1/payroll/settings", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["auto_release_enabled"] is True
    assert data["auto_release_day"] == 8


async def test_disabled_auto_release_is_not_due(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _setup(client, settings, seed)  # auto_release_enabled defaults to False
    assert await _service(db, settings).is_auto_release_due() is False


async def test_release_for_system_releases_previous_month_idempotently(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _setup(client, settings, seed)

    # Nothing released yet: the employee can't see the previous month.
    before = await client.get(
        f"/api/v1/payroll/payslips/{_PREV}", headers=auth_headers(settings, seed.report)
    )
    assert before.status_code == 404

    run = await _service(db, settings).release_for_system()
    assert run is not None
    assert run.period_month == _PREV
    assert run.employee_count >= 1
    await db.commit()

    # Now the employee sees + can read the released previous month.
    after = await client.get(
        f"/api/v1/payroll/payslips/{_PREV}", headers=auth_headers(settings, seed.report)
    )
    assert after.status_code == 200

    # Running again is a no-op (already released -> never re-emails).
    assert await _service(db, settings).release_for_system() is None
