"""Avora task-recurrence scheduler - makes a recurring task actually recur.

`tasks.cadence` was a label nothing read. 509 live tasks say DAILY, WEEKLY or
MONTHLY and not one had ever produced a second instance, so people re-created
the work by hand every morning. Each tick, when enabled, every live recurring
series that has no instance due today or later gets exactly one more.

Idempotent by construction: the check is "does this series already have an
instance due on or after today", so running hourly, or re-running after the
worker was down for a week, produces ONE task rather than twenty-four or
seven. Nothing is ever back-dated - `next_due` rolls forward to today - so
switching this on against a year of history creates tomorrow's task, not last
January's.

A daily series skips non-working days, read from `AttendancePolicy`. A weekly
or monthly one does not: its day is the agreement.

Deploy alongside the API (ONE instance - two would race on the same series).
Env:
  DATABASE_URL                       Postgres URL (same as the API).
  TASK_RECURRENCE_ENABLED            "true" to turn it on (default off).
  TASK_RECURRENCE_TICK_SECONDS       seconds between sweeps (default 3600).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.recurrence import WorkingCalendar
from app.db.session import SessionFactory, engine
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.task import TaskRepository
from app.services.task_recurrence_service import TaskRecurrenceService
from worker.heartbeat import beat, record

log = logging.getLogger("task_recurrence_scheduler")

HEARTBEAT_ENV = "HEARTBEAT_URL_TASK_RECURRENCE"

#: Fallback when no attendance policy row exists yet: Mon-Fri. Better than
#: "every day", which would put a daily task on everybody's Sunday.
_DEFAULT_WORKING_DAYS = frozenset({0, 1, 2, 3, 4})

#: How far ahead to load holidays. A daily task moves forward days, not
#: months, and a long Diwali or Christmas closure is comfortably inside this.
_HOLIDAY_LOOKAHEAD_DAYS = 30


def _build_service(session: AsyncSession) -> TaskRecurrenceService:
    """Wire the service the same way the FastAPI DI graph would."""
    return TaskRecurrenceService(
        TaskRepository(session),
        EmployeeRepository(session),
        AuditRepository(session),
        LeaveRepository(session),
    )


async def _org_context(session: AsyncSession) -> tuple[date, WorkingCalendar]:
    """Today in the ORG's timezone, and the days the office is open.

    The org's date, not the server's: a sweep running at 02:00 UTC is already
    the next day in IST, and generating against the wrong date would shift
    every series by one.
    """
    policy = await AttendancePolicyRepository(session).get()
    zone = ZoneInfo(policy.timezone) if policy is not None else UTC
    today = datetime.now(UTC).astimezone(zone).date()

    per_week = policy.working_days_per_week if policy is not None else 5
    # A 6-day week is Mon-Sat, 7 is everything. Derived rather than stored as
    # a set, because that is the shape the policy actually holds.
    weekdays = frozenset(range(min(max(int(per_week), 1), 7))) or _DEFAULT_WORKING_DAYS

    # A month either side: a daily task only ever moves forward a few days,
    # and loading the whole holiday table every tick to find them would be
    # wasteful on a calendar that spans years.
    closed = await HolidayRepository(session).dates_in_range(
        today - timedelta(days=1), today + timedelta(days=_HOLIDAY_LOOKAHEAD_DAYS)
    )
    return today, WorkingCalendar(weekdays=weekdays, closed=frozenset(closed))


async def _tick() -> None:
    settings = get_settings()
    if not settings.task_recurrence_enabled:
        return
    async with SessionFactory() as session:
        try:
            today, calendar_ = await _org_context(session)
            outcome = await _build_service(session).run_due(today, calendar_)
            # Commit BEFORE logging success: the created rows are what make
            # the next sweep a no-op, so they must be durable before we say so.
            await session.commit()
            if outcome.total or outcome.needs_a_due_date:
                log.info(
                    "recurrence: %d created, %d already current, %d stopped, "
                    "%d cannot recur (no due date)",
                    outcome.created,
                    outcome.already_current,
                    len(outcome.stopped),
                    outcome.needs_a_due_date,
                )
        except Exception:
            await session.rollback()
            raise


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Read through Settings, not os.getenv: a bare float() on "1h" would
    # restart-loop with a traceback naming nothing, and "0" is a tight spin.
    tick = max(60.0, float(get_settings().task_recurrence_tick_seconds))
    log.info("Avora task-recurrence scheduler starting (tick=%.0fs)", tick)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)
                await record("task-recurrence")  # tick succeeded → report liveness
            except Exception as exc:  # keep the loop alive across transient failures
                # Class name only - an exception can carry row data (§5.6).
                log.warning("tick failed: %s", type(exc).__name__)
            await asyncio.sleep(tick)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
