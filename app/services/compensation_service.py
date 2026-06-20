"""Compensation business rules — the tightest authorization in the app.

Per policy (confirmed by the product owner): compensation is visible to **HR,
Admin, and the person themselves only**. Managers, senior managers, it_admin,
executives and viewers may NOT see anyone's pay — not even their reports'.
Writes are HR/Admin only. Every read and write is audited (Security rule 5.7).
"""

from __future__ import annotations

import uuid

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.compensation import Compensation
from app.models.employee import Role
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.compensation import CompensationWrite


def _can_manage(caller: CurrentUser) -> bool:
    """HR/Admin may read anyone's and write anyone's compensation."""
    return caller.role in (Role.ADMIN, Role.HR)


class CompensationService:
    def __init__(
        self,
        compensation: CompensationRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._compensation = compensation
        self._employees = employees
        self._audit = audit

    def _assert_can_view(self, caller: CurrentUser, employee_id: uuid.UUID) -> None:
        # HR/Admin see all; everyone else only their own. No manager carve-out.
        if not _can_manage(caller) and caller.employee_id != employee_id:
            raise AuthorizationError()

    async def get(self, caller: CurrentUser, employee_id: uuid.UUID) -> Compensation:
        self._assert_can_view(caller, employee_id)
        record = await self._compensation.get_for_employee(employee_id)
        if record is None:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.read",
            target=f"employee:{employee_id}",
        )
        return record

    async def set(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: CompensationWrite
    ) -> Compensation:
        # Writing pay is an HR/Admin action only — never the person themselves.
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        record = await self._compensation.upsert(employee_id, data, updated_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.update",
            target=f"employee:{employee_id}",
        )
        return record
