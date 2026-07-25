"""Avora End-of-Day scheduler — generates and sends daily work summaries.

Runs as its own process (like `worker/payroll_scheduler.py`), reusing the app's
async session + services. Each tick, when EOD is enabled: at/after the configured
local hour it generates a draft for every present employee (idempotent per
employee/day via the unique `eod_reports` constraint), and always auto-sends any
draft left unreviewed past the cutoff.

Deploy alongside the API (one instance). Env:
  DATABASE_URL            Postgres URL (asyncpg-style, same as the API).
  EOD_ENABLED             "true" to turn the feature on (default off).
  OPENROUTER_API_KEY      so generation can call the LLM.
  EOD_MODEL               OpenRouter model id, e.g. "anthropic/claude-sonnet-4.5".
  EMAIL_PROVIDER          "sendgrid" (default) or "smtp" — the delivery transport.
  SENDGRID_API_KEY / SMTP_*  credentials for the chosen provider, so approved/
                          auto-sent reports can be delivered.
  EOD_TICK_SECONDS        seconds between checks (default 900 = 15 min).
  HEARTBEAT_URL_EOD       optional: pinged after each successful tick (Better Stack).

The schedule (on/off + draft/send times) is read from the DB (`eod_settings`,
editable in Settings → EOD timing), not env — changes apply on the next tick.
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
from app.repositories.eod_report import EodReportRepository
from app.repositories.eod_settings import EodSettingsRepository
from app.repositories.notification import NotificationRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.eod_service import EodService
from app.services.eod_settings_service import EodSettingsService
from app.services.llm_service import LlmService
from app.services.notification_service import NotificationService

log = logging.getLogger("eod_scheduler")

HEARTBEAT_ENV = "HEARTBEAT_URL_EOD"

TICK_SECONDS = float(os.getenv("EOD_TICK_SECONDS", "900"))


def _build_service(session: AsyncSession) -> EodService:
    """Wire an EodService the same way the FastAPI DI graph does."""
    settings = get_settings()
    audit = AuditRepository(session)
    employees = EmployeeRepository(session)
    eod_settings = EodSettingsService(EodSettingsRepository(session), audit, settings)
    policy = AttendancePolicyService(AttendancePolicyRepository(session), audit)
    attendance = AttendanceService(
        employees,
        ActivityRepository(session),
        WorkSessionRepository(session),
        policy,
        RegularizationRepository(session),
    )
    return EodService(
        EodReportRepository(session),
        employees,
        TaskRepository(session),
        ActivityRepository(session),
        ScreenshotRepository(session),
        attendance,
        policy,
        LlmService(settings),
        EmailService(settings),
        NotificationService(NotificationRepository(session)),
        audit,
        eod_settings,
        settings,
    )


async def _tick() -> None:
    settings = get_settings()
    # Env half of the gate (LLM key + model). The on/off switch + schedule are in
    # the DB and applied inside run_due, so toggling EOD needs no redeploy.
    if not settings.eod_secrets_present:
        return
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        try:
            generated, sent, purged_activity, purged_shots = await _build_service(session).run_due(
                now
            )
            if generated or sent or purged_activity or purged_shots:
                log.info(
                    "generated %d draft(s), auto-sent %d, purged %d activity row(s), "
                    "%d screenshot(s)",
                    generated,
                    sent,
                    purged_activity,
                    purged_shots,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Avora EOD scheduler starting (tick=%.0fs)", TICK_SECONDS)
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
