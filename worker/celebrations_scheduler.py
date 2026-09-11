"""Avora celebrations scheduler — daily birthday / anniversary / festival emails.

Runs as its own process (like `worker/payroll_scheduler.py`), reusing the app's
async session + services. Each tick it computes today's date in the org's
attendance-policy timezone and calls `CelebrationService.run_daily`, which is
idempotent (via `celebration_settings.last_run_on`) — so extra ticks and
restarts never re-send a day's greetings.

Deploy alongside the API (one instance). Env:
  DATABASE_URL              Postgres URL (asyncpg-style, same as the API).
  SENDGRID_API_KEY          so greetings can actually be delivered.
  EMAIL_FROM                sender address (defaults to the app's configured value).
  CELEBRATIONS_TICK_SECONDS seconds between checks (default 900 = every 15 min).
  CELEBRATIONS_HOUR         local hour to send at (default 12, i.e. noon).
  CELEBRATIONS_MINUTE       minute past that hour (default 0).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.celebration_settings import CelebrationSettingsRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.festival import FestivalRepository
from app.repositories.holiday import HolidayRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.celebration_service import CelebrationService
from app.services.email_service import EmailService
from worker.heartbeat import beat

log = logging.getLogger("celebrations_scheduler")

# 15 minutes, so the noon send is punctual rather than up to an hour late.
TICK_SECONDS = float(os.getenv("CELEBRATIONS_TICK_SECONDS", "900"))
HEARTBEAT_ENV = "HEARTBEAT_URL_CELEBRATIONS"


def _build_service(session: AsyncSession) -> CelebrationService:
    return CelebrationService(
        CelebrationSettingsRepository(session),
        FestivalRepository(session),
        EmployeeRepository(session),
        EmailService(get_settings()),
        AuditRepository(session),
        HolidayRepository(session),
    )


async def _tick() -> None:
    async with SessionFactory() as session:
        try:
            spec = await AttendancePolicyService(
                AttendancePolicyRepository(session), AuditRepository(session)
            ).spec()
            settings = get_settings()
            now_local = datetime.now(UTC).astimezone(ZoneInfo(spec.timezone))
            send_after = settings.celebrations_hour * 60 + settings.celebrations_minute
            if now_local.hour * 60 + now_local.minute < send_after:
                # Too early. Nothing is skipped: `run_daily` is idempotent on the
                # date, so the first tick past noon still sends the whole day —
                # including when the worker was restarted after noon.
                return
            today = now_local.date()
            sent = await _build_service(session).run_daily(today)
            if sent:
                log.info("celebration run for %s sent %d emails", today, sent)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Avora celebrations scheduler starting (tick=%.0fs)", TICK_SECONDS)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)
            except Exception as exc:  # keep the loop alive across transient failures
                log.warning("tick failed: %s", exc)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
