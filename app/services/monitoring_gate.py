"""Monitoring gate — should we accept monitoring ingest for this employee now?

We capture ONLY while the employee is actively checked in — i.e. there is an
OPEN work session (clocked in, not yet clocked out) — and only on a working day
(per the attendance policy's working-days-per-week). Everything outside that
window is dropped and stored as nothing:
  - before check-in (no session open yet),
  - after checkout (session closed, by any source — dashboard, auto, biometric),
  - non-working days (e.g. Sunday / the configured weekend).

Enforcement is server-side on purpose (the agent is untrusted, Security rule
5.4); the device may keep posting, but the trusted layer drops it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService


class MonitoringGateService:
    def __init__(
        self, work_sessions: WorkSessionRepository, policy: AttendancePolicyService
    ) -> None:
        self._sessions = work_sessions
        self._policy = policy

    async def should_suppress(self, employee_id: uuid.UUID, now: datetime | None = None) -> bool:
        """True ⇒ drop this monitoring ingest (store nothing). Capture is allowed
        only during an open work session on a working day; before check-in, after
        checkout, and on non-working days (e.g. Sunday) we suppress. `now` is
        injectable for tests; it defaults to the current time."""
        spec = await self._policy.spec()
        now_local = (now or datetime.now(UTC)).astimezone(ZoneInfo(spec.timezone))
        # Non-working day: weekday() is 0=Mon … 6=Sun; indices ≥ working_days_per_week
        # are off (5 ⇒ Sat & Sun off, 6 ⇒ Sun off).
        if now_local.weekday() >= spec.working_days_per_week:
            return True
        # Only capture while clocked in. No open session ⇒ before check-in or after
        # checkout ⇒ suppress.
        return await self._sessions.get_open(employee_id) is None
