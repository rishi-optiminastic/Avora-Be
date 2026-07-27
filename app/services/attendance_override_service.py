"""Attendance override business rules — HR/Admin manual day corrections.

Only HR/Admin manage overrides (no approval; audited). The attendance computation
reads them via the repository and forces the day's status; the payroll present/
absent counts follow. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.attendance_override import AttendanceOverride
from app.models.employee import Role
from app.repositories.attendance_override import AttendanceOverrideRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.attendance_override import AttendanceOverrideCreate
from app.schemas.auth import CurrentUser


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class AttendanceOverrideService:
    def __init__(
        self,
        overrides: AttendanceOverrideRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._overrides = overrides
        self._employees = employees
        self._audit = audit

    async def list_for_period(
        self, caller: CurrentUser, period_month: str
    ) -> Sequence[AttendanceOverride]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return await self._overrides.list_for_period(period_month)

    async def upsert(
        self, caller: CurrentUser, payload: AttendanceOverrideCreate
    ) -> AttendanceOverride:
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(payload.employee_id)
        if employee is None or not employee.is_active:
            raise ValidationError("Unknown or inactive employee.")
        row = await self._overrides.upsert(payload, created_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.override.set",
            target=f"override:{row.id}:{payload.employee_id}:{payload.day}:{payload.status.value}",
        )
        return row

    async def delete(self, caller: CurrentUser, override_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        deleted = await self._overrides.delete(override_id)
        if not deleted:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.override.delete",
            target=f"override:{override_id}",
        )
