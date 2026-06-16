"""Ping business rules.

Issuing a ping is a manager/admin action against someone in the caller's scope
(out-of-scope targets get 404, never leaking existence — rule 5.3). The agent
inbox is device-authenticated; fetching marks the pings delivered so they aren't
replayed on the next poll.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.ping import Ping
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.ping import PingRepository
from app.schemas.auth import CurrentDevice, CurrentUser
from app.schemas.ping import PingCreate


class PingService:
    def __init__(
        self, pings: PingRepository, employees: EmployeeRepository, audit: AuditRepository
    ) -> None:
        self._pings = pings
        self._employees = employees
        self._audit = audit

    async def issue(self, caller: CurrentUser, payload: PingCreate) -> Ping:
        if not caller.is_manager:
            raise AuthorizationError()
        if not await self._employees.can_read(caller, payload.employee_id):
            raise NotFoundError()
        message = payload.message.strip() if payload.message else None
        ping = await self._pings.create(
            target_employee_id=payload.employee_id,
            issued_by_id=caller.employee_id,
            message=message or None,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="ping.issue",
            target=f"employee:{payload.employee_id}",
        )
        return ping

    async def inbox(self, device: CurrentDevice) -> Sequence[Ping]:
        pending = await self._pings.pending_for_employee(device.employee_id)
        if pending:
            await self._pings.mark_delivered([p.id for p in pending], datetime.now(UTC))
        return pending
