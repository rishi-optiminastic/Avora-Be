"""Device business rules.

Enrolling / revoking a device is an admin- or IT-admin-only action (Security
rule 5.3). The raw token is generated here and returned exactly once; only its
peppered hash is stored (rule 5.2). Reads are scoped in the repository.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.security import generate_device_token, hash_device_token
from app.models.device import Device
from app.models.employee import Role
from app.repositories.audit import AuditRepository
from app.repositories.device import DeviceRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.device import DeviceCreate, DeviceSelfEnroll


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.IT_ADMIN)


def _derive_label(payload: DeviceSelfEnroll) -> str:
    """Build a human label from the agent's hints; fall back to a generic one."""
    if payload.label and payload.label.strip():
        return payload.label.strip()[:256]
    parts = [p.strip() for p in (payload.hostname, payload.os) if p and p.strip()]
    return " · ".join(parts)[:256] if parts else "New device"


class DeviceService:
    def __init__(
        self,
        settings: Settings,
        devices: DeviceRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._settings = settings
        self._devices = devices
        self._employees = employees
        self._audit = audit

    async def list_for_caller(self, caller: CurrentUser) -> Sequence[Device]:
        return await self._devices.list_for_scope(caller)

    async def enroll(self, caller: CurrentUser, payload: DeviceCreate) -> tuple[Device, str]:
        """Returns (device, raw_token). The raw token is shown to the admin once."""
        if not _can_manage(caller):
            raise AuthorizationError()
        # The target must be a real employee (avoids enrolling to a stray id).
        if await self._employees.get(payload.employee_id) is None:
            raise NotFoundError()

        raw_token = generate_device_token()
        device = await self._devices.create(
            employee_id=payload.employee_id,
            label=payload.label,
            token_hash=hash_device_token(self._settings, raw_token),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="device.enroll",
            target=f"device:{device.id}:employee:{payload.employee_id}",
        )
        return device, raw_token

    async def self_enroll(
        self, caller: CurrentUser, payload: DeviceSelfEnroll
    ) -> tuple[Device, str]:
        """The agent enrolls the caller's own machine. No role gate — any
        authenticated employee may enroll a device for themselves only; it's
        bound to caller.employee_id, never a client-supplied id (rule #2)."""
        raw_token = generate_device_token()
        
        device = await self._devices.create(
            employee_id=caller.employee_id,
            label=_derive_label(payload),
            token_hash=hash_device_token(self._settings, raw_token),
        )
        await self._audit.append(
            actor=str(caller.employee_idd),
            action="device.self_enroll",
            target=f"device:{device.id}:employee:{caller.employee_id}",
        )
        return device, raw_token

    async def revoke(self, caller: CurrentUser, device_id: uuid.UUID) -> Device:
        if not _can_manage(caller):
            raise AuthorizationError()
        device = await self._devices.get(device_id)
        if device is None:
            raise NotFoundError()
        await self._devices.revoke(device)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="device.revoke",
            target=f"device:{device_id}",
        )
        return device
