"""Work-session business rules — the employee's own clock-in / clock-out.

Self-service only: a caller clocks themselves in/out (the morning login and the
evening logout). One open session at a time; clocking in while already clocked in
is idempotent (returns the open session). Every action is audited.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import ValidationError
from app.models.work_session import WorkSession
from app.repositories.audit import AuditRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.auth import CurrentUser


class WorkSessionService:
    def __init__(self, sessions: WorkSessionRepository, audit: AuditRepository) -> None:
        self._sessions = sessions
        self._audit = audit

    async def clock_in(self, caller: CurrentUser, *, ip_address: str | None = None) -> WorkSession:
        open_session = await self._sessions.get_open(caller.employee_id)
        if open_session is not None:
            return open_session  # already clocked in — idempotent
        session = await self._sessions.create(
            employee_id=caller.employee_id,
            clock_in_at=datetime.now(UTC),
            source="dashboard",
            ip_address=ip_address,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.clock_in",
            target=f"session:{session.id}",
        )
        return session

    async def clock_out(self, caller: CurrentUser) -> WorkSession:
        session = await self._sessions.get_open(caller.employee_id)
        if session is None:
            raise ValidationError("You are not clocked in.")
        session.clock_out_at = datetime.now(UTC)
        session.clock_out_source = "dashboard"
        await self._sessions.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.clock_out",
            target=f"session:{session.id}",
        )
        return session

    async def current(self, caller: CurrentUser) -> WorkSession | None:
        return await self._sessions.get_open(caller.employee_id)
