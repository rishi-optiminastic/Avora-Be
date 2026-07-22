"""Employee data access. The ONLY place employee queries are built.

All queries are parameterized through the ORM (Security rule 5.6). Scope-aware
helpers (`list_for_scope`) take the caller's identity and constrain rows to what
that caller may see (Golden rule #3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.names import clean_display_name, is_placeholder_name
from app.models.employee import Employee, EmployeeStatus, Gender, Role, TrackingMode
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

    async def get_by_biometric_id(self, biometric_id: str) -> Employee | None:
        """Resolve an employee by their attendance-device enrollment id."""
        result = await self._session.execute(
            select(Employee).where(Employee.biometric_id == biometric_id)
        )
        return result.scalar_one_or_none()

    async def tracking_mode(self, employee_id: uuid.UUID) -> TrackingMode:
        """The ingest gate reads this on every sample — a single indexed lookup."""
        mode = await self._session.scalar(
            select(Employee.tracking_mode).where(Employee.id == employee_id)
        )
        return mode or TrackingMode.WORK

    async def set_tracking_mode(self, employee: Employee, mode: TrackingMode) -> None:
        employee.tracking_mode = mode
        await self._session.flush()

    async def update_profile(
        self,
        employee: Employee,
        *,
        full_name: str,
        job_title: str | None,
        timezone: str | None,
        date_of_birth: date,
        gender: Gender,
    ) -> None:
        employee.full_name = full_name
        employee.job_title = job_title
        employee.timezone = timezone
        employee.date_of_birth = date_of_birth
        employee.gender = gender
        await self._session.flush()

    # The only profile attributes an admin/HR PATCH may set (role/email/identity
    # keys are deliberately excluded — see AdminProfileUpdate). The whitelist
    # guards setattr against ever touching anything outside this set.
    _ADMIN_PROFILE_FIELDS = frozenset(
        {
            "full_name",
            "department",
            "location",
            "job_title",
            "manager_id",
            "timezone",
            "hire_date",
            "uan_number",
            "date_of_birth",
            "gender",
            "biometric_id",
        }
    )

    async def admin_update_profile(self, employee: Employee, fields: dict[str, object]) -> None:
        """Apply ONLY the supplied profile fields (PATCH semantics — keys the client
        omitted are left untouched, so a single-field edit can't blank the rest)."""
        for key, value in fields.items():
            if key in self._ADMIN_PROFILE_FIELDS:
                setattr(employee, key, value)
        await self._session.flush()

    async def set_active(self, employee: Employee, is_active: bool) -> None:
        employee.is_active = is_active
        await self._session.flush()

    async def set_avatar(
        self,
        employee: Employee,
        *,
        object_key: str | None,
        content: bytes | None,
        content_type: str,
    ) -> None:
        employee.avatar_object_key = object_key
        employee.avatar_content = content
        employee.avatar_content_type = content_type
        await self._session.flush()

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

    async def readmit_from_invite(
        self, employee: Employee, *, role: Role, department: str | None
    ) -> None:
        """Re-admit a previously de-activated (offboarded) employee when they
        accept a fresh admin invite. Without this, an accepted invite would leave
        an existing is_active=False row untouched — the backend 401s every request
        for an inactive employee and the invite-only gate rejects re-sign-in.
        Reactivating + re-applying the invited role/department is the in-PMS,
        admin-authorised re-admission path (mirrors create_from_invite, rule 5.5)."""
        employee.is_active = True
        employee.status = EmployeeStatus.ACTIVE
        employee.role = role
        if department is not None:
            employee.department = department
        await self._session.flush()

    async def refresh_display_name(self, employee: Employee, provider_name: str | None) -> None:
        """Replace an email-derived placeholder with the provider's real name.

        Idempotent and conservative: does nothing unless the provider gave a
        usable name AND the stored name is still the auto-generated placeholder,
        so a curated or HR-synced name is never overwritten.
        """
        cleaned = clean_display_name(provider_name)
        if cleaned is None or cleaned == employee.full_name:
            return
        if not is_placeholder_name(employee.full_name, employee.work_email):
            return
        employee.full_name = cleaned
        await self._session.flush()

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

    async def all_in_scope(self, caller: CurrentUser) -> Sequence[Employee]:
        """Every active employee visible to the caller (no pagination) — used by
        attendance/activity rollups that must include people with zero samples."""
        clauses = self._scope_clause(caller)
        clauses.append(Employee.is_active.is_(True))
        rows = await self._session.execute(
            select(Employee).where(*clauses).order_by(Employee.full_name)
        )
        return rows.scalars().all()

    async def list_by_role(self, role: Role) -> Sequence[Employee]:
        """All active employees with a given role (e.g. admins, for EOD recipients)."""
        rows = await self._session.execute(
            select(Employee)
            .where(Employee.role == role, Employee.is_active.is_(True))
            .order_by(Employee.full_name)
        )
        return rows.scalars().all()

    async def list_all_active(self) -> Sequence[Employee]:
        """Every active employee (org-wide) — used by the celebration broadcast."""
        rows = await self._session.execute(
            select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name)
        )
        return rows.scalars().all()

    async def get_many(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Employee]:
        """Batch lookup keyed by id — avoids per-row `get()` in loops."""
        if not ids:
            return {}
        rows = await self._session.execute(select(Employee).where(Employee.id.in_(ids)))
        return {e.id: e for e in rows.scalars().all()}

    async def upsert_from_hr(
        self,
        *,
        hr_external_id: str,
        work_email: str,
        full_name: str,
        department: str | None,
        manager_id: uuid.UUID | None,
        status: EmployeeStatus,
        biometric_id: str | None = None,
        hire_date: date | None = None,
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
        # Only overwrite the biometric id when HR actually sends one (keep any
        # value an admin set manually otherwise).
        if biometric_id is not None:
            employee.biometric_id = biometric_id
        # Same conservative rule for the joining date: only set it when HR sends
        # one, so a manually-corrected hire date is not wiped by a later sync.
        if hire_date is not None:
            employee.hire_date = hire_date
        await self._session.flush()
        return employee

    async def set_role(self, employee: Employee, role: Role) -> Employee:
        """Admin-only privilege change."""
        employee.role = role
        await self._session.flush()
        return employee
