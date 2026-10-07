"""Avora task-escalation scheduler - makes an overdue task progressively louder.

Runs as its own process (like `worker/auto_checkout_scheduler.py`), reusing the
app's async session + services. Each tick, when enabled:

    overdue >= TASK_ESCALATION_WARN_AFTER_DAYS     -> warn the assignee (pop-up)
    overdue >= TASK_ESCALATION_MANAGER_AFTER_DAYS  -> add the reporting manager
                                                      as a collaborator
    overdue >= TASK_ESCALATION_ADMIN_AFTER_DAYS    -> add an admin as a collaborator

Each tier fires exactly once per task (`tasks.escalation_level`), so the sweep is
idempotent - running it hourly, or re-running after a crash, never re-notifies or
re-adds anyone. Completing a task stops it escalating immediately.

Deploy alongside the API (ONE instance - two would race on the same tasks). Env:
  DATABASE_URL                          Postgres URL (same as the API).
  TASK_ESCALATION_ENABLED               "true" to turn it on (default off).
  TASK_ESCALATION_TICK_SECONDS          seconds between sweeps (default 3600).
  TASK_ESCALATION_WARN_AFTER_DAYS       default 1
  TASK_ESCALATION_MANAGER_AFTER_DAYS    default 3
  TASK_ESCALATION_ADMIN_AFTER_DAYS      default 6
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.task import TaskRepository
from app.services.notification_service import NotificationService
from app.services.task_escalation_service import TaskEscalationService
from worker.heartbeat import beat

log = logging.getLogger("task_escalation_scheduler")

HEARTBEAT_ENV = "HEARTBEAT_URL_TASK_ESCALATION"


def _build_service(session: AsyncSession) -> TaskEscalationService:
    """Wire a TaskEscalationService the same way the FastAPI DI graph does."""
    settings = get_settings()
    return TaskEscalationService(
        TaskRepository(session),
        EmployeeRepository(session),
        NotificationService(NotificationRepository(session)),
        AuditRepository(session),
        warn_after_days=settings.task_escalation_warn_after_days,
        manager_after_days=settings.task_escalation_manager_after_days,
        admin_after_days=settings.task_escalation_admin_after_days,
    )


async def _tick() -> None:
    settings = get_settings()
    if not settings.task_escalation_enabled:
        return
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        try:
            outcome = await _build_service(session).run_due(now)
            # Commit BEFORE logging success: the escalation level is what makes
            # this idempotent, so it must be durable before we claim it happened.
            await session.commit()
            if outcome.total:
                log.info(
                    "escalated: %d warned, %d manager(s) added, %d admin(s) added",
                    outcome.assignee_warned,
                    outcome.managers_added,
                    outcome.admins_added,
                )
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Read through Settings, not os.getenv: a bare float() on a malformed value
    # ("1h") raised before logging existed, so the container restart-looped with a
    # traceback naming nothing, and "0" became a tight spin over the whole table.
    tick = max(60.0, float(get_settings().task_escalation_tick_seconds))
    log.info("Avora task-escalation scheduler starting (tick=%.0fs)", tick)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)  # tick succeeded → report liveness
            except Exception as exc:  # keep the loop alive across transient failures
                # Class name only - an exception can carry row data (§5.6).
                log.warning("tick failed: %s", type(exc).__name__)
            await asyncio.sleep(tick)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
