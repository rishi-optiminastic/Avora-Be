"""Leave-allocation business rules — HR/Admin manage per-employee overrides.

Mirrors `CompensationService`'s authorization shape, but read is also HR/Admin
only (unlike pay, an employee's effective quota is already visible to them via
`LeaveService.balance()` — this endpoint exposes the raw override record for
the admin editing surface, not a self-service view).
"""

from __future__ import annotations

import uuid

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Role
from app.models.leave_allocation import LeaveAllocation
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave_allocation import LeaveAllocationRepository
from app.schemas.auth import CurrentUser
from app.schemas.leave_allocation import LeaveAllocationWrite


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class LeaveAllocationService:
    def __init__(
        self,
        allocations: LeaveAllocationRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._allocations = allocations
        self._employees = employees
        self._audit = audit

    async def get(self, caller: CurrentUser, employee_id: uuid.UUID) -> LeaveAllocation:
        if not _can_manage(caller):
            raise AuthorizationError()
        record = await self._allocations.get_for_employee(employee_id)
        if record is None:
            raise NotFoundError()
        return record

    async def set(
        self, caller: CurrentUser, employee_id: uuid.UUID, payload: LeaveAllocationWrite
    ) -> LeaveAllocation:
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        record = await self._allocations.upsert(employee_id, payload, updated_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave_allocation.update",
            target=f"employee:{employee_id}",
        )
        return record
