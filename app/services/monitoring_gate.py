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

`state_for` / `states_for` return the same verdict with its REASON kept, which
is what the admin fleet view reads. A dropped upload still marks the device
alive (`ActivityService._advance_device`), so "the agent is reporting" and "we
are recording" are genuinely different facts, and anything that shows the first
without the second is telling an admin all is well while nothing is stored.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.payroll import is_working_day
from app.repositories.work_session import WorkSessionRepository
from app.schemas.monitoring import CaptureWindowState
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
        return await self.state_for(employee_id, now) is not CaptureWindowState.CAPTURING

    async def state_for(
        self, employee_id: uuid.UUID, now: datetime | None = None
    ) -> CaptureWindowState:
        """Why this employee's uploads are (or are not) being stored right now.

        The same decision `should_suppress` makes, with the reason kept instead of
        flattened to a bool, so an admin reading the fleet can be told "they have
        not clocked in" rather than left to wonder why a live agent produced no
        screenshots. One rule, two callers (Security rule 5.1).
        """
        states = await self.states_for([employee_id], now)
        return states[employee_id]

    async def states_for(
        self, employee_ids: Sequence[uuid.UUID], now: datetime | None = None
    ) -> dict[uuid.UUID, CaptureWindowState]:
        """`state_for` across a roster, in a fixed two queries rather than per
        person. Duplicate ids collapse; an empty roster asks nothing."""
        ids = list(dict.fromkeys(employee_ids))
        if not ids:
            return {}
        spec = await self._policy.spec()
        now_local = (now or datetime.now(UTC)).astimezone(ZoneInfo(spec.timezone))
        # Non-working day — including the alternate Saturdays that are weekly offs,
        # are off (5 ⇒ Sat & Sun off, 6 ⇒ Sun off). Nobody is captured, so there is
        # no point asking who is clocked in.
        if not is_working_day(now_local.date(), spec.working_days_per_week):
            return dict.fromkeys(ids, CaptureWindowState.NON_WORKING_DAY)
        # Only capture while clocked in. No open session ⇒ before check-in or after
        # checkout ⇒ suppress.
        clocked_in = await self._sessions.open_employee_ids(ids)
        return {
            employee_id: (
                CaptureWindowState.CAPTURING
                if employee_id in clocked_in
                else CaptureWindowState.NOT_CLOCKED_IN
            )
            for employee_id in ids
        }
