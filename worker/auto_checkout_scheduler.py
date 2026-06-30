"""Avora auto-checkout scheduler — closes sessions people forgot to clock out of.

Runs as its own process (like `worker/eod_scheduler.py`), reusing the app's
async session + services. Each tick, when enabled: after the office window
closes it stamps a clock-out (at the PC's last-activity time) on any session
left open, marks it `source="auto"`, and emails the person. Prior-day open
sessions are always closed. Idempotent — a closed session won't reappear.

Deploy alongside the API (one instance). Env:
  DATABASE_URL                       Postgres URL (same as the API).
  AUTO_CHECKOUT_ENABLED              "true" to turn it on (default off).
  AUTO_CHECKOUT_TICK_SECONDS         seconds between sweeps (default 900).
  AUTO_CHECKOUT_IDLE_GRACE_MINUTES   quiet-this-long ⇒ PC is off (default 30).
  SENDGRID_API_KEY                   so the forgot-checkout email can be sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from worker.heartbeat import beat

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.auto_checkout_service import AutoCheckoutService
from app.services.email_service import EmailService

log = logging.getLogger("auto_checkout_scheduler")

TICK_SECONDS = float(os.getenv("AUTO_CHECKOUT_TICK_SECONDS", "900"))
HEARTBEAT_ENV = "HEARTBEAT_URL_AUTOCHECKOUT"


def _build_service(session: AsyncSession) -> AutoCheckoutService:
    """Wire an AutoCheckoutService the same way the FastAPI DI graph does."""
    settings = get_settings()
    audit = AuditRepository(session)
    policy = AttendancePolicyService(AttendancePolicyRepository(session), audit)
    return AutoCheckoutService(
        WorkSessionRepository(session),
        ActivityRepository(session),
        EmployeeRepository(session),
        policy,
        EmailService(settings),
        audit,
        settings,
    )


async def _tick() -> None:
    settings = get_settings()
    if not settings.auto_checkout_enabled:
        return
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        try:
            closed = await _build_service(session).run_due(now)
            if closed:
                log.info("auto-closed %d session(s)", closed)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Avora auto-checkout scheduler starting (tick=%.0fs)", TICK_SECONDS)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)  # tick succeeded → report liveness
            except Exception as exc:  # keep the loop alive across transient failures
                log.warning("tick failed: %s", exc)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
