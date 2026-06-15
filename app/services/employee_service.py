"""Employee business rules.

Services contain authorization and business logic but NO FastAPI objects
(Layering §4). Every read is scoped to the caller; every privilege change is
admin-only and audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Employee, Role
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser


class EmployeeService:
    def __init__(self, employees: EmployeeRepository, audit: AuditRepository) -> None:
        self._employees = employees
        self._audit = audit

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

    async def set_role(self, caller: CurrentUser, target_id: uuid.UUID, role: Role) -> Employee:
        # Privilege changes happen only inside PMS, by an admin (rule 5.5).
        if not caller.is_admin:
            raise AuthorizationError()
        employee = await self._employees.get(target_id)
        if employee is None:
            raise NotFoundError()
        updated = await self._employees.set_role(employee, role)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="employee.role_change",
            target=f"employee:{target_id}:{role.value}",
        )
        return updated
