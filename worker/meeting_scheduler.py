"""Avora Founder Meetings scheduler - the three things nobody is awake to do.

Each tick, when enabled:

  * Retries bookings the calendar refused. Both approvers already said yes to
    these and nothing else recovers them: no invitation exists to remind anyone
    the meeting was supposed to happen, so without this they are lost silently.
  * Reminds the requester and the participants about a meeting starting inside
    the configured lead time. The CEO is left out - Google is already
    reminding them, and a second nudge per meeting is how somebody ends up
    turning notifications off.
  * Marks meetings COMPLETED once their end time has passed.

Idempotent by construction. A retry reuses the existing event row rather than
inserting a second one (the unique constraint on `meeting_events` is the
concurrency guard); a reminder stamps `reminder_sent_at` and drops out of the
window; completing a meeting moves it out of CONFIRMED. Running every five
minutes, or catching up after a week down, does each thing once.

Deploy alongside the API, ONE instance - two would race on the same booking.

Env:
  DATABASE_URL                      Postgres URL (same as the API).
  MEETING_SCHEDULER_ENABLED         "true" to turn it on (default off).
  MEETING_SCHEDULER_TICK_SECONDS    seconds between sweeps (default 300).
  HEARTBEAT_URL_MEETINGS            optional Better Stack ping.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.employee import EmployeeRepository
from app.repositories.meeting_request import MeetingRequestRepository
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.repositories.notification import NotificationRepository
from app.services.meeting_booking_service import MeetingBookingService
from app.services.meeting_scheduler_service import MeetingSchedulerService
from app.services.notification_service import NotificationService
from worker.heartbeat import beat, record

log = logging.getLogger("meeting_scheduler")

HEARTBEAT_ENV = "HEARTBEAT_URL_MEETINGS"

#: Chrome's extension badge and the dashboard both read from the API, so this
#: cadence only bounds how late a reminder can be, not how stale a screen is.
#: Five minutes against a lead time measured in tens of minutes.
_MIN_TICK_SECONDS = 60.0


def _build_service(session: AsyncSession) -> MeetingSchedulerService:
    """Wire the service the same way the FastAPI DI graph would."""
    settings = get_settings()
    requests = MeetingRequestRepository(session)
    employees = EmployeeRepository(session)
    meeting_settings = MeetingSettingsRepository(session)
    return MeetingSchedulerService(
        requests,
        meeting_settings,
        MeetingBookingService(settings, requests, employees, meeting_settings),
        NotificationService(NotificationRepository(session)),
    )


async def _tick() -> None:
    if not get_settings().meeting_scheduler_enabled:
        return
    async with SessionFactory() as session:
        try:
            outcome = await _build_service(session).run_tick()
            # Commit BEFORE logging success: the stamped reminders and the
            # COMPLETED rows are what make the next sweep a no-op, so they
            # have to be durable before we claim the work was done.
            await session.commit()
            if outcome.did_anything:
                log.info(
                    "meetings: %d retried (%d recovered), %d reminded, %d completed, %d problems",
                    outcome.retried,
                    outcome.rebooked,
                    outcome.reminded,
                    outcome.completed,
                    len(outcome.failures),
                )
            for failure in outcome.failures:
                # Already reduced to a class name by the service - an
                # exception can carry row data (be/CLAUDE.md 5.6).
                log.warning("meetings: %s", failure)
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Read through Settings, not os.getenv: a bare float() on "5m" would
    # restart-loop with a traceback naming nothing, and "0" is a tight spin.
    tick = max(_MIN_TICK_SECONDS, float(get_settings().meeting_scheduler_tick_seconds))
    log.info("Avora meeting scheduler starting (tick=%.0fs)", tick)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)
                await record("meetings")  # tick succeeded -> report liveness
            except Exception as exc:  # keep the loop alive across transient failures
                log.warning("tick failed: %s", type(exc).__name__)
            await asyncio.sleep(tick)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
