"""Agent-fleet nudge.

A manager/admin nudges an employee to keep (or reinstall) the desktop agent. The
channel is chosen by whether the agent is actually reachable:
- if a device reported within ONLINE_WINDOW → an on-screen agent ping;
- otherwise (gone/closed/never enrolled) → an in-app notification + a reinstall
  email, because an agent ping would silently never arrive.

Authorization mirrors PingService.issue: manager+ and the target must be in the
caller's scope (out-of-scope → 404, rule 5.3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.logging import get_logger
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.ping import PingKind
from app.repositories.audit import AuditRepository
from app.repositories.device import DeviceRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.ping import PingRepository
from app.schemas.auth import CurrentUser
from app.services.email_service import EmailError, EmailService
from app.services.notification_service import NotificationService

logger = get_logger("app.agent_nudge")

ONLINE_WINDOW = timedelta(minutes=15)
REINSTALL_PATH = "/dashboard/download"
_DEFAULT_PING = "Please keep the Avora agent running."
_REINSTALL_BODY = "Your Avora agent isn't reporting. Please reinstall it to resume tracking."

NudgeChannel = Literal["agent", "notification_email"]


class AgentNudgeService:
    def __init__(
        self,
        employees: EmployeeRepository,
        devices: DeviceRepository,
        pings: PingRepository,
        notifications: NotificationService,
        email: EmailService,
        audit: AuditRepository,
    ) -> None:
        self._employees = employees
        self._devices = devices
        self._pings = pings
        self._notifications = notifications
        self._email = email
        self._audit = audit

    async def nudge(
        self, caller: CurrentUser, employee_id: uuid.UUID, message: str | None = None
    ) -> NudgeChannel:
        if not caller.is_manager:
            raise AuthorizationError()
        if not await self._employees.can_read(caller, employee_id):
            raise NotFoundError()
        employee = await self._employees.get(employee_id)
        if employee is None:
            raise NotFoundError()

        note = message.strip() if message else None
        online = await self._is_online(employee_id)
        channel: NudgeChannel = "agent" if online else "notification_email"

        if channel == "agent":
            await self._pings.create(
                target_employee_id=employee_id,
                issued_by_id=caller.employee_id,
                message=note or _DEFAULT_PING,
                kind=PingKind.PING,
            )
        else:
            await self._notifications.notify(
                recipient_id=employee_id,
                kind=NotificationKind.SYSTEM,
                level=NotificationLevel.WARNING,
                title="Reinstall the Avora agent",
                body=note or _REINSTALL_BODY,
                link=REINSTALL_PATH,
                actor_id=caller.employee_id,
            )
            try:
                await self._email.send_agent_reinstall(
                    to=employee.work_email,
                    employee_name=employee.full_name,
                    link_path=REINSTALL_PATH,
                )
            except EmailError:
                # The in-app notification already landed; don't fail the request
                # just because email is down (status only, never the address).
                logger.warning("agent_reinstall_email_failed", extra={"employee": str(employee_id)})

        await self._audit.append(
            actor=str(caller.employee_id),
            action="device.nudge",
            target=f"employee:{employee_id}:{channel}",
        )
        return channel

    async def broadcast_update(self, caller: CurrentUser) -> int:
        """Tell every enrolled agent to self-update now — a fleet-wide 'update'
        command each agent acts on at its next poll (~15s). Device-fleet op, so
        admin / IT-admin only (rule 5.3). Returns how many agents were signalled."""
        if caller.role not in (Role.ADMIN, Role.IT_ADMIN):
            raise AuthorizationError()
        devices = await self._devices.list_for_scope(caller)  # admin/IT scope = all
        employee_ids = {d.employee_id for d in devices if not d.is_revoked}
        for employee_id in employee_ids:
            await self._pings.create(
                target_employee_id=employee_id,
                issued_by_id=caller.employee_id,
                message=None,
                kind=PingKind.UPDATE,
            )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="agent.update_broadcast",
            target=f"devices:{len(employee_ids)}",
        )
        return len(employee_ids)

    async def _is_online(self, employee_id: uuid.UUID) -> bool:
        now = datetime.now(UTC)
        for device in await self._devices.list_for_employee(employee_id):
            if device.is_revoked or device.last_seen_at is None:
                continue
            seen = device.last_seen_at
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            if now - seen <= ONLINE_WINDOW:
                return True
        return False
