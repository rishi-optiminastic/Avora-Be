"""Avora forms scheduler - sends the onboarding and probation forms on their day.

Runs as its own process (like `worker/task_escalation_scheduler.py`). Each tick:

1. Create today's automatic assignments: the new-joiner form N days after a
   joiner's start date, the buddy form M days after it (to the buddy, or held
   as "needs a buddy" when none is on file). Buddy forms that were waiting go
   out once a buddy appears. The three probation review forms open
   `trigger_days` before each probation end date (the manager one waits as
   "needs a manager" when none is on file). Committed on its own.
2. Tell each assignee (email + in-app), including forms HR sent by hand.
   Each one is its own transaction, so a mail failure never undoes an
   assignment and a crash part-way through never mails anyone twice.
3. Tell HR, once, about joiners with no buddy and probationers with no manager.

One assignment per (trigger, joiner) ever, so ticks are idempotent: running
hourly, or again after a crash, never sends anyone a form twice.

Deploy alongside the API (ONE instance). Env:
  DATABASE_URL         Postgres URL (same as the API).
  SENDGRID_API_KEY     or SMTP settings, so the emails can go out.
  APP_BASE_URL         for the links in the emails.
  FORMS_TICK_SECONDS   seconds between runs (default 3600).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormAssignmentRepository, FormRepository, FormResponseRepository
from app.repositories.leave_policy import LeavePolicyRepository
from app.repositories.notification import NotificationRepository
from app.repositories.playbook import PlaybookRepository, PlaybookRunRepository
from app.repositories.probation_checklist import ProbationChecklistRepository
from app.repositories.probation_decision import ProbationDecisionRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.services.email_service import EmailService
from app.services.form_assignment_service import FormAssignmentService
from app.services.notification_service import NotificationService
from app.services.playbook_service import PlaybookService, SweepOutcome
from app.services.probation_forms import ProbationFormLink
from app.services.untracked_work_service import UntrackedWorkService
from worker.heartbeat import beat, record

log = logging.getLogger("forms_scheduler")

HEARTBEAT_ENV = "HEARTBEAT_URL_FORMS"
# Postgres advisory-lock key held for the length of a tick (any fixed number
# unique to this worker).
_LOCK_KEY = 7_351_904_226
_MIN_TICK_SECONDS = 60.0


def _build_detector(session: AsyncSession) -> UntrackedWorkService:
    """Wire an UntrackedWorkService the same way the FastAPI DI graph does."""
    return UntrackedWorkService(
        ActivityRepository(session),
        ScreenshotRepository(session),
        WorkEntityRepository(session),
        TaskRepository(session),
        EmployeeRepository(session),
        AuditRepository(session),
    )


def _build_playbooks(session: AsyncSession) -> PlaybookService:
    """Wire a PlaybookService the same way the FastAPI DI graph does."""
    return PlaybookService(
        PlaybookRepository(session),
        PlaybookRunRepository(session),
        EmployeeRepository(session),
        AuditRepository(session),
    )


def _build_service(session: AsyncSession) -> FormAssignmentService:
    """Wire a FormAssignmentService the same way the FastAPI DI graph does."""
    settings = get_settings()
    return FormAssignmentService(
        FormRepository(session),
        FormAssignmentRepository(session),
        FormResponseRepository(session),
        EmployeeRepository(session),
        NotificationService(NotificationRepository(session)),
        EmailService(settings),
        AuditRepository(session),
        settings,
        ProbationFormLink(
            ProbationChecklistRepository(session),
            LeavePolicyRepository(session),
            AttendancePolicyRepository(session),
            EmployeeRepository(session),
            ProbationDecisionRepository(session),
        ),
    )


async def _step(name: str, work: Callable[[FormAssignmentService], Awaitable[object]]) -> None:
    """Run one phase in its own transaction."""
    async with SessionFactory() as session:
        try:
            outcome = await work(_build_service(session))
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    if outcome:
        log.info("%s: %s", name, outcome)


async def _tick() -> None:
    """Runs only while holding the forms lock: two schedulers overlapping (a
    deploy, a second copy) would otherwise both create and both mail."""
    async with engine.connect() as lock_conn:
        got = await lock_conn.scalar(select(func.pg_try_advisory_lock(_LOCK_KEY)))
        if not got:
            log.info("another forms scheduler is running; skipping this tick")
            return
        try:
            await _run_phases()
        finally:
            await lock_conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            await lock_conn.commit()


async def _run_phases() -> None:
    # The org's day, not the server's: forms open on the org's calendar.
    async with SessionFactory() as session:
        today = await _build_service(session).org_today()
    # Phase 1 commits before anyone is told, so an assignment is durable before
    # its email can go out.
    await _step("assigned", lambda s: s.run_due(today))
    await _notify_each(today)
    await _step("hr alerted about joiners without a buddy", lambda s: s.alert_missing_buddies())
    await _step(
        "hr alerted about probationers without a manager", lambda s: s.alert_missing_managers()
    )
    await _run_playbooks(today)
    await _detect_untracked(datetime.now(UTC))


async def _run_playbooks(today: date) -> None:
    """Run every playbook trigger whose day is today.

    **Why it lives in this worker.** A playbook fires off an employee's
    lifecycle date, which is exactly what this scheduler already does for
    forms - same trigger shape, same cadence, same org-day calculation. It
    also means no new Coolify resource: the workers that needed one sat
    undeployed for weeks, and a feature nobody runs is worth nothing.

    **Its own session and its own try/except**, deliberately. Playbooks are
    the newer, less-proven half; a failure here must never roll back an
    assignment or stop a probation form going out. Logged by class name only,
    since an exception can carry row data (§5.6).
    """
    # One transaction per sweep, not one for all three: a monthly checklist
    # failing must not roll back a joiner who was onboarded correctly in the
    # same tick.
    sweeps: tuple[tuple[str, Callable[[PlaybookService], Awaitable[SweepOutcome]]], ...] = (
        ("joiners", lambda s: s.sweep_new_joiners(today)),
        ("offboarding", lambda s: s.sweep_offboarding(today)),
        ("monthly", lambda s: s.sweep_monthly(today)),
    )
    for name, run in sweeps:
        try:
            async with SessionFactory() as session:
                outcome = await run(_build_playbooks(session))
                await session.commit()
        except Exception as exc:
            log.warning("playbook sweep %s failed: %s", name, type(exc).__name__)
            continue
        if outcome.runs or outcome.skipped:
            log.info(
                "playbooks/%s: %d run, %d tasks, %d already done, %d skipped",
                name,
                outcome.runs,
                outcome.tasks,
                outcome.already_done,
                len(outcome.skipped),
            )
            for line in outcome.skipped[:10]:
                log.info("playbook step skipped: %s", line)


async def _detect_untracked(now: datetime) -> None:
    """Raise tasks for work happening on screen with no task behind it.

    Gated on UNTRACKED_DETECTION_ENABLED and off by default. This is the only
    thing in Avora that writes a task with no human in the loop, so it is a
    deliberate switch rather than a consequence of deploying.

    Its own session and its own try/except, like the playbook sweeps: the
    newest and least-proven path must not be able to roll back a form
    assignment or a joiner's checklist.
    """
    if not get_settings().untracked_detection_enabled:
        return
    try:
        async with SessionFactory() as session:
            outcome = await _build_detector(session).sweep(now)
            await session.commit()
    except Exception as exc:
        log.warning("untracked-work sweep failed: %s", type(exc).__name__)
        return
    log.info(
        "untracked: %d people considered, %d tasks raised, %d held back by the cap",
        outcome.considered,
        outcome.created,
        outcome.held_back,
    )
    # The notes are how an empty catalog or an unmatchable entity becomes
    # visible instead of looking like "nobody worked on anything".
    for note in outcome.notes:
        log.info("untracked: %s", note)
    for title in outcome.titles[:10]:
        log.info("untracked raised: %s", title)


async def _notify_each(today: date) -> None:
    """One transaction per person told: a crash part-way through keeps the
    record of everyone already mailed, so nobody is mailed twice."""
    async with SessionFactory() as session:
        pending = await _build_service(session).unnotified_ids(today)
    told = 0
    for assignment_id in pending:
        try:
            async with SessionFactory() as session:
                told += int(await _build_service(session).notify_one(assignment_id))
                await session.commit()
        except Exception as exc:  # one bad row must not stop the rest
            log.warning("notify failed for one assignment: %s", type(exc).__name__)
    if told:
        log.info("notified: %d", told)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tick = max(_MIN_TICK_SECONDS, float(get_settings().forms_tick_seconds))
    log.info("Avora forms scheduler starting (tick=%.0fs)", tick)
    try:
        while True:
            try:
                await _tick()
                await beat(HEARTBEAT_ENV)
                await record("forms")
            except Exception as exc:  # keep the loop alive across transient failures
                log.warning("tick failed: %s", type(exc).__name__)
            await asyncio.sleep(tick)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
