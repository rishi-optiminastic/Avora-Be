"""Leave-allocation business rules — HR/Admin manage per-employee overrides.

Mirrors `CompensationService`'s authorization shape, but read is also HR/Admin
only (unlike pay, an employee's effective quota is already visible to them via
`LeaveService.balance()` — this endpoint exposes the raw override record for
the admin editing surface, not a self-service view).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.leave_year import leave_year_window
from app.models.employee import Role
from app.models.leave_allocation import LeaveAllocation
from app.repositories.attendance_policy import AttendancePolicyRepository
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
        attendance_policy: AttendancePolicyRepository,
    ) -> None:
        self._allocations = allocations
        self._employees = employees
        self._audit = audit
        self._attendance_policy = attendance_policy

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
        record = await self._allocations.upsert(
            employee_id,
            payload,
            updated_by=caller.employee_id,
            used_before_year_start=await self._used_before_year_start(payload),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave_allocation.update",
            target=f"employee:{employee_id}",
        )
        return record

    async def _used_before_year_start(self, payload: LeaveAllocationWrite) -> date | None:
        """The leave year HR's "taken before Avora" days belong to: the org's
        current one, on the org's clock. None when none were entered, so a
        quota-only override carries no year at all."""
        if not payload.used_before_entered():
            return None
        policy = await self._attendance_policy.get()
        zone = ZoneInfo(policy.timezone) if policy is not None else UTC
        return leave_year_window(datetime.now(UTC).astimezone(zone).date())[0]
