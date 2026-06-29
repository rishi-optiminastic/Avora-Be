"""Auto-checkout — close work sessions someone forgot to clock out of.

A session left open (no clock-out) would otherwise accumulate "worked" time
forever (the 195h bug). After the auto-checkout trigger time (5 PM local, a
buffer past the office window), this finds open sessions whose PC has gone quiet
(no activity for the idle grace ⇒ the machine is off),
stamps the clock-out at that last-activity time (≈ when the PC turned off),
marks it `clock_out_source="auto"`, and emails the person. Prior-day sessions
still open are always closed. Runs from `worker/auto_checkout_scheduler.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.models.work_session import WorkSession
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.email_service import EmailError, EmailService

log = logging.getLogger("auto_checkout")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AutoCheckoutService:
    def __init__(
        self,
        sessions: WorkSessionRepository,
        activity: ActivityRepository,
        employees: EmployeeRepository,
        policy: AttendancePolicyService,
        email: EmailService,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._activity = activity
        self._employees = employees
        self._policy = policy
        self._email = email
        self._audit = audit
        self._settings = settings

    async def run_due(self, now: datetime) -> int:
        """Close every forgotten open session that's due. Returns how many."""
        open_sessions = await self._sessions.list_open_before(now)
        if not open_sessions:
            return 0

        spec = await self._policy.spec()
        tz = ZoneInfo(spec.timezone)
        now = _aware(now)
        now_local = now.astimezone(tz)
        today = now_local.date()
        now_minute = now_local.hour * 60 + now_local.minute

        # We hold off on today's sessions until the configured trigger time (5 PM
        # local), separate from the attendance work-end used for the fallback stamp.
        trigger_minute = (
            self._settings.auto_checkout_hour * 60 + self._settings.auto_checkout_minute
        )

        closed = 0
        for session in open_sessions:
            checkout = await self._checkout_time(
                session, tz, today, now, now_minute, spec.work_end_minute, trigger_minute
            )
            if checkout is None:
                continue  # not due yet (today, still within hours or PC still active)
            session.clock_out_at = checkout
            session.clock_out_source = "auto"
            await self._sessions.flush()
            closed += 1
            await self._notify(session, checkout, tz)
            await self._audit.append(
                actor="system:auto-checkout",
                action="attendance.auto_checkout",
                target=f"session:{session.id}",
            )
        if closed:
            log.info("auto-closed %d forgotten session(s)", closed)
        return closed

    async def _checkout_time(
        self,
        session: WorkSession,
        tz: ZoneInfo,
        today: date,
        now: datetime,
        now_minute: int,
        work_end_minute: int,
        trigger_minute: int,
    ) -> datetime | None:
        """When to clock this open session out, or None if it isn't due yet."""
        cin = _aware(session.clock_in_at)
        local_date = cin.astimezone(tz).date()
        if local_date > today:  # future row — leave it
            return None

        y, m, d = local_date.year, local_date.month, local_date.day
        midnight = datetime(y, m, d, tzinfo=tz)
        day_start = midnight.astimezone(UTC)
        day_end = (midnight + timedelta(days=1)).astimezone(UTC)
        window_end = (midnight + timedelta(minutes=work_end_minute)).astimezone(UTC)

        aggs = await self._activity.daily_aggregates([session.employee_id], day_start, day_end)
        agg = aggs.get(session.employee_id)
        last_seen = _aware(agg.logout_at) if agg is not None else None

        if local_date == today:
            if now_minute < trigger_minute:
                return None  # before the trigger time (5 PM) — give them the buffer
            grace = timedelta(minutes=self._settings.auto_checkout_idle_grace_minutes)
            if last_seen is not None and (now - last_seen) < grace:
                return None  # PC still active → person is still working
            checkout = last_seen if last_seen is not None else window_end
        else:
            # A prior day left open: always close it (use last activity, else window end).
            checkout = last_seen if last_seen is not None else window_end

        # Never before the clock-in, never in the future.
        return min(max(checkout, cin), now)

    async def _notify(self, session: WorkSession, checkout: datetime, tz: ZoneInfo) -> None:
        employee = await self._employees.get(session.employee_id)
        if employee is None or not employee.work_email:
            return
        local = checkout.astimezone(tz)
        day_label = local.strftime("%b %-d")
        checkout_label = local.strftime("%-I:%M %p")
        try:
            await self._email.send_forgot_checkout(
                to=employee.work_email,
                employee_name=employee.full_name,
                day_label=day_label,
                checkout_label=checkout_label,
            )
        except EmailError as exc:  # email is best-effort — the checkout still stands
            log.warning("forgot-checkout email failed for %s: %s", session.employee_id, exc)
