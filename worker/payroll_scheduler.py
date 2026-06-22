"""Avora payroll scheduler — sends the monthly HR salary digest on the pay day.

Runs as its own process (like `worker/ocr_worker.py`), but reuses the app's
async session + services (it needs the EmailService and the same salary maths).
Each tick it checks the org's payroll settings: if auto-send is on and today (in
the attendance-policy timezone) is the configured `pay_day_of_month`, it sends
this month's digest exactly once — `PayrollService.run_for_system` is idempotent
via the unique `payroll_runs.period_month`, so extra ticks are safe.

Deploy alongside the API (one instance). Env:
  DATABASE_URL          Postgres URL (asyncpg-style, same as the API).
  SENDGRID_API_KEY      so the digest can actually be delivered.
  EMAIL_FROM            sender address (defaults to the app's configured value).
  PAYROLL_TICK_SECONDS  seconds between checks (default 3600 = hourly).
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.payroll_run import PayrollRunRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.payroll_service import PayrollService

log = logging.getLogger("payroll_scheduler")

TICK_SECONDS = float(os.getenv("PAYROLL_TICK_SECONDS", "3600"))


def _build_service(session: AsyncSession) -> PayrollService:
    """Wire a PayrollService the same way the FastAPI DI graph does."""
    audit = AuditRepository(session)
    employees = EmployeeRepository(session)
    policy = AttendancePolicyService(AttendancePolicyRepository(session), audit)
    attendance = AttendanceService(
        employees,
        ActivityRepository(session),
        WorkSessionRepository(session),
        policy,
        RegularizationRepository(session),
    )
    return PayrollService(
        PayrollSettingsRepository(session),
        PayrollRunRepository(session),
        CompensationRepository(session),
        employees,
        attendance,
        policy,
        LeaveRepository(session),
        HolidayRepository(session),
        EmailService(get_settings()),
        audit,
    )


async def _tick() -> None:
    async with SessionFactory() as session:
        try:
            service = _build_service(session)
            if await service.is_auto_send_due():
                run = await service.run_for_system()
                if run is not None:
                    log.info(
                        "payroll digest sent for %s (%d employees)",
                        run.period_month,
                        run.employee_count,
                    )
                else:
                    log.info("auto-send due but already sent this month (or no recipients)")
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Avora payroll scheduler starting (tick=%.0fs)", TICK_SECONDS)
    try:
        while True:
            try:
                await _tick()
            except Exception as exc:  # keep the loop alive across transient failures
                log.warning("tick failed: %s", exc)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
