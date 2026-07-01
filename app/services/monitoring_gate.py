"""Monitoring gate — should we accept monitoring ingest for this employee now?

Privacy: once an employee deliberately checks out for the day (the dashboard
"I'm leaving" clock-out, or the auto-checkout worker), we stop STORING their
activity samples and screenshots until they clock back in — even if their
machine keeps sending. A biometric out-punch (which may just be a lunch break)
does NOT count, and employees who never formally check in (WFH / agent-only)
are never suppressed.

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

    async def is_checked_out(self, employee_id: uuid.UUID) -> bool:
        """True ⇒ the employee deliberately checked out today and has not clocked
        back in, so their monitoring ingest should be dropped."""
        # Currently clocked in (open session) → always monitor.
        if await self._sessions.get_open(employee_id) is not None:
            return False
        # Otherwise only a *deliberate* checkout on the current LOCAL day suppresses
        # monitoring — so yesterday's auto-checkout never bleeds into today, and a
        # biometric lunch out-punch never counts.
        spec = await self._policy.spec()
        day_start = self._local_day_start_utc(spec.timezone)
        return await self._sessions.has_deliberate_checkout_since(employee_id, day_start)

    @staticmethod
    def _local_day_start_utc(tz: str) -> datetime:
        now_local = datetime.now(UTC).astimezone(ZoneInfo(tz))
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(UTC)
