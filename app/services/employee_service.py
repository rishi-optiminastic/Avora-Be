"""Employee business rules.

Services contain authorization and business logic but NO FastAPI objects
(Layering §4). Every read is scoped to the caller; every privilege change is
admin-only and audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.employee import Employee, Role, TrackingMode
from app.models.ping import PingKind
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.ping import PingRepository
from app.schemas.auth import CurrentUser
from app.schemas.employee import AdminProfileUpdate

logger = get_logger("app.employee")

_MODE_COMMAND = {
    TrackingMode.WORK: PingKind.MODE_WORK,
    TrackingMode.PERSONAL: PingKind.MODE_PERSONAL,
}

ALLOWED_AVATAR_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB — matches the UI hint.


class EmployeeService:
    def __init__(
        self,
        employees: EmployeeRepository,
        pings: PingRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._employees = employees
        self._pings = pings
        self._audit = audit
        self._settings = settings

    async def get_for_caller(self, caller: CurrentUser, target_id: uuid.UUID) -> Employee:
        # Prefer 404 over 403 so we don't reveal existence to an out-of-scope
        # caller (API conventions §7).
        if not await self._employees.can_read(caller, target_id):
            raise NotFoundError()
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.read",
            target=f"employee:{target_id}",
        )
        return employee

    async def get_self(self, caller: CurrentUser) -> Employee:
        """The caller's own record — always in scope, no audit needed."""
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        return employee

    async def list_for_caller(
        self, caller: CurrentUser, *, offset: int, limit: int
    ) -> tuple[Sequence[Employee], int]:
        return await self._employees.list_for_scope(caller, offset=offset, limit=limit)

    async def set_my_avatar(self, caller: CurrentUser, data: bytes, content_type: str) -> Employee:
        """The employee uploads their own profile photo (image, ≤2 MB)."""
        media = (content_type or "").split(";")[0].strip().lower()
        if media not in ALLOWED_AVATAR_TYPES:
            raise ValidationError("Photo must be a PNG, JPG, or WebP image.")
        if not data:
            raise ValidationError("Photo is empty.")
        if len(data) > MAX_AVATAR_BYTES:
            raise ValidationError("Photo is too large (max 2 MB).")

        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()

        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.avatar_object_key(str(caller.employee_id), media)
            try:
                await storage.put_object(object_key, data, media)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("avatar_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        await self._employees.set_avatar(
            employee, object_key=object_key, content=stored, content_type=media
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.avatar",
            target=f"employee:{caller.employee_id}",
        )
        return employee

    async def update_self_profile(
        self, caller: CurrentUser, *, full_name: str, job_title: str | None, timezone: str | None
    ) -> Employee:
        """The employee edits their own display fields (name/title/timezone)."""
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        await self._employees.update_profile(
            employee,
            full_name=full_name.strip(),
            job_title=(job_title or None),
            timezone=(timezone or None),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.profile_update",
            target=f"employee:{caller.employee_id}",
        )
        return employee

    async def get_avatar(self, caller: CurrentUser, employee_id: uuid.UUID) -> Employee:
        """An employee's profile photo — visible to any signed-in colleague (a
        directory photo, not sensitive). 404 when there's no photo."""
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.has_avatar:
            raise NotFoundError()
        return employee

    async def set_tracking_mode(self, caller: CurrentUser, mode: TrackingMode) -> Employee:
        """The employee toggles their own work/personal mode. We persist it (the
        ingest gate's source of truth) and queue a command so their agent reacts
        promptly. Even if the agent never gets the command, the server gate still
        drops anything captured while PERSONAL — that's the real guarantee."""
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        await self._employees.set_tracking_mode(employee, mode)
        await self._pings.create(
            target_employee_id=caller.employee_id,
            issued_by_id=caller.employee_id,
            message=None,
            kind=_MODE_COMMAND[mode],
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="tracking.mode_change",
            target=f"employee:{caller.employee_id}:{mode.value}",
        )
        return employee

    # ---- admin / HR user management --------------------------------------- #
    # Profile, status, and tracking changes for OTHER employees are admin OR HR.
    # Role changes are NOT here — they stay admin-only in `set_role` (rule 5.5).
    @staticmethod
    def _require_user_manager(caller: CurrentUser) -> None:
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()

    async def admin_update_profile(
        self, caller: CurrentUser, target_id: uuid.UUID, payload: AdminProfileUpdate
    ) -> Employee:
        self._require_user_manager(caller)
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        # PATCH: write only the fields the client actually sent (a name-only edit
        # must not blank department/manager/hire_date). An explicit null DOES clear
        # a field — that's how the editor removes a manager or timezone.
        fields = payload.model_dump(exclude_unset=True)
        manager_id = fields.get("manager_id")
        if manager_id is not None:
            if manager_id == target_id:
                raise ValidationError("An employee cannot report to themselves.")
            if await self._employees.get(manager_id) is None:
                raise ValidationError("Manager not found.")
        if "full_name" in fields:
            fields["full_name"] = str(fields["full_name"]).strip()
        for key in ("department", "location", "job_title", "timezone", "biometric_id"):
            if key in fields and isinstance(fields[key], str):
                fields[key] = fields[key].strip() or None
        await self._employees.admin_update_profile(employee, fields)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.admin_profile_update",
            target=f"employee:{target_id}",
        )
        return employee

    async def set_avatar_for(
        self, caller: CurrentUser, target_id: uuid.UUID, data: bytes, content_type: str
    ) -> Employee:
        """Admin/HR uploads another employee's profile photo (image, ≤2 MB). Mirrors
        `set_my_avatar` but scoped to a target id; the storage key stays server-assigned."""
        self._require_user_manager(caller)
        media = (content_type or "").split(";")[0].strip().lower()
        if media not in ALLOWED_AVATAR_TYPES:
            raise ValidationError("Photo must be a PNG, JPG, or WebP image.")
        if not data:
            raise ValidationError("Photo is empty.")
        if len(data) > MAX_AVATAR_BYTES:
            raise ValidationError("Photo is too large (max 2 MB).")

        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()

        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.avatar_object_key(str(target_id), media)
            try:
                await storage.put_object(object_key, data, media)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("avatar_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        await self._employees.set_avatar(
            employee, object_key=object_key, content=stored, content_type=media
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.admin_avatar",
            target=f"employee:{target_id}",
        )
        return employee

    async def set_active(
        self, caller: CurrentUser, target_id: uuid.UUID, is_active: bool
    ) -> Employee:
        self._require_user_manager(caller)
        if target_id == caller.employee_id and not is_active:
            # Don't let an admin/HR lock themselves out.
            raise ValidationError("You can't deactivate your own account.")
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        await self._employees.set_active(employee, is_active)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.activate" if is_active else "employee.deactivate",
            target=f"employee:{target_id}",
        )
        return employee

    async def set_tracking_mode_for(
        self, caller: CurrentUser, target_id: uuid.UUID, mode: TrackingMode
    ) -> Employee:
        """Admin/HR sets another employee's capture mode (the self path is
        `set_tracking_mode`). Persists the gate's source of truth + commands the agent."""
        self._require_user_manager(caller)
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        await self._employees.set_tracking_mode(employee, mode)
        await self._pings.create(
            target_employee_id=target_id,
            issued_by_id=caller.employee_id,
            message=None,
            kind=_MODE_COMMAND[mode],
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="tracking.mode_change",
            target=f"employee:{target_id}:{mode.value}",
        )
        return employee

    async def set_role(self, caller: CurrentUser, target_id: uuid.UUID, role: Role) -> Employee:
        # Privilege changes happen only inside PMS, by an admin (rule 5.5).
        if not caller.is_admin:
            raise AuthorizationError()
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        # Don't let an admin demote themselves out of admin — mirrors the
        # self-deactivation guard. Since only an admin can reach this path, blocking
        # self-demotion guarantees at least one admin always remains (no lockout).
        if (
            target_id == caller.employee_id
            and employee.role is Role.ADMIN
            and role is not Role.ADMIN
        ):
            raise ValidationError("You can't remove your own admin role — ask another admin.")
        updated = await self._employees.set_role(employee, role)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.role_change",
            target=f"employee:{target_id}:{role.value}",
        )
        return updated
