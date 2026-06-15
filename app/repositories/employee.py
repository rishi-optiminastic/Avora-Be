"""Employee data access. The ONLY place employee queries are built.

All queries are parameterized through the ORM (Security rule 5.6). Scope-aware
helpers (`list_for_scope`) take the caller's identity and constrain rows to what
that caller may see (Golden rule #3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmployeeStatus, Role
from app.schemas.auth import CurrentUser


class EmployeeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self._session.get(Employee, employee_id)

    async def get_by_external_id(self, hr_external_id: str) -> Employee | None:
        result = await self._session.execute(
            select(Employee).where(Employee.hr_external_id == hr_external_id)
        )
        return result.scalar_one_or_none()

    async def get_by_work_email(self, work_email: str) -> Employee | None:
        result = await self._session.execute(
            select(Employee).where(Employee.work_email == work_email)
        )
        return result.scalar_one_or_none()

    async def create_from_invite(
        self, *, work_email: str, full_name: str, role: Role, department: str | None = None
    ) -> Employee:
        """Provision an employee when an admin invite is accepted.

        Unlike the HR upsert, this DOES set the role — accepting an admin-issued
        invite is the in-PMS, admin-authorised path that grants it (rule 5.5).
        The synthetic hr_external_id keeps the unique key satisfied until/if HR
        later syncs this person.
        """
        employee = Employee(
            hr_external_id=f"invite:{uuid.uuid4()}",
            work_email=work_email,
            full_name=full_name,
            role=role,
            department=department,
            status=EmployeeStatus.ACTIVE,
            is_active=True,
        )
        self._session.add(employee)
        await self._session.flush()
        return employee

    def _scope_clause(self, caller: CurrentUser) -> list[ColumnElement[bool]]:
        """Row-level scope: what may THIS caller read? (Security rule 5.3)

        - admin / hr: whole org.
        - senior_manager: their whole department.
        - manager: themselves and their direct reports.
        - executive / it_admin / viewer / employee: only themselves.

        Scope is derived from the caller's server-side record, never from a
        client-supplied field. `senior_manager` resolves the caller's department
        with a correlated subquery so we needn't widen the token/CurrentUser.
        """
        if caller.role in (Role.ADMIN, Role.HR):
            return []
        if caller.role is Role.SENIOR_MANAGER:
            caller_department = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            return [Employee.department == caller_department]
        if caller.role is Role.MANAGER:
            return [
                (Employee.manager_id == caller.employee_id) | (Employee.id == caller.employee_id)
            ]
        return [Employee.id == caller.employee_id]

    async def can_read(self, caller: CurrentUser, target_id: uuid.UUID) -> bool:
        stmt = select(Employee.id).where(Employee.id == target_id, *self._scope_clause(caller))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_scope(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        include_inactive: bool = False,
    ) -> tuple[Sequence[Employee], int]:
        clauses = self._scope_clause(caller)
        if not include_inactive:
            clauses.append(Employee.is_active.is_(True))

        base = select(Employee).where(*clauses)
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self._session.execute(
            base.order_by(Employee.full_name).offset(offset).limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def upsert_from_hr(
        self,
        *,
        hr_external_id: str,
        work_email: str,
        full_name: str,
        department: str | None,
        manager_id: uuid.UUID | None,
        status: EmployeeStatus,
    ) -> Employee:
        """Create or update from HR. Never touches `role` (rule 5.5)."""
        employee = await self.get_by_external_id(hr_external_id)
        if employee is None:
            employee = Employee(
                hr_external_id=hr_external_id,
                role=Role.EMPLOYEE,  # default only on create; never from HR
            )
            self._session.add(employee)

        employee.work_email = work_email
        employee.full_name = full_name
        employee.department = department
        employee.manager_id = manager_id
        employee.status = status
        employee.is_active = status is EmployeeStatus.ACTIVE
        await self._session.flush()
        return employee

    async def set_role(self, employee: Employee, role: Role) -> Employee:
        """Admin-only privilege change."""
        employee.role = role
        await self._session.flush()
        return employee
