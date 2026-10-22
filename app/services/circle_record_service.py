"""Reads of what Avora mirrors from Circle: personal and employment details,
appraisal history and assets.

Who may see them: the person themselves, and HR/Admin or a payroll-manager
grant holder (`can_manage_payroll`). Nobody else - not a manager, not even for
their own reports: these carry contact details, ID numbers and salaries, and a
manager's existing view of their team never included them. An out-of-scope
read is 403 (the person's existence is not secret, their details are), the
same rule as compensation. Sensitive reads are audited.

Read-only: everything here is written only by the Circle sync.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.pii_crypto import decrypt_pii
from app.models.circle_record import EmployeeAppraisal, EmployeeAsset, EmployeeCircleProfile
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.circle_record import CircleRecordRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.circle import CircleProfileRead
from app.services.circle_ownership import circle_edit_url


class CircleRecordService:
    def __init__(
        self,
        records: CircleRecordRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._records = records
        self._employees = employees
        self._audit = audit
        self._settings = settings

    async def _authorized(self, caller: CurrentUser, employee_id: uuid.UUID) -> Employee:
        if not caller.can_manage_payroll and caller.employee_id != employee_id:
            raise AuthorizationError()
        employee = await self._employees.get(employee_id)
        if employee is None:
            raise NotFoundError()
        return employee

    async def _audit_read(self, caller: CurrentUser, action: str, employee_id: uuid.UUID) -> None:
        await self._audit.append(
            actor=str(caller.employee_id), action=action, target=f"employee:{employee_id}"
        )

    async def profile(self, caller: CurrentUser, employee_id: uuid.UUID) -> CircleProfileRead:
        employee = await self._authorized(caller, employee_id)
        record = await self._records.get_profile(employee_id)
        await self._audit_read(caller, "circle_profile.read", employee_id)
        return self._to_read(employee, record)

    async def appraisals(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> Sequence[EmployeeAppraisal]:
        await self._authorized(caller, employee_id)
        rows = await self._records.list_appraisals(employee_id)
        await self._audit_read(caller, "appraisals.read", employee_id)
        return rows

    async def assets(self, caller: CurrentUser, employee_id: uuid.UUID) -> Sequence[EmployeeAsset]:
        await self._authorized(caller, employee_id)
        return await self._records.list_assets(employee_id)

    def _to_read(
        self, employee: Employee, record: EmployeeCircleProfile | None
    ) -> CircleProfileRead:
        linked = employee.circle_linked
        return CircleProfileRead(
            linked=linked,
            circle_url=circle_edit_url(self._settings, employee) if linked else None,
            synced_at=record.synced_at if record else None,
            employee_code=employee.employee_number,
            work_location=employee.location,
            employment_type=record.employment_type if record else None,
            phone=self._plain(record, "phone_encrypted"),
            personal_email=self._plain(record, "personal_email_encrypted"),
            address=self._plain(record, "address_encrypted"),
            emergency_contact=self._plain(record, "emergency_contact_encrypted"),
            pan_masked=record.pan_masked if record else None,
            aadhaar_masked=record.aadhaar_masked if record else None,
            uan_masked=record.uan_masked if record else None,
        )

    def _plain(self, record: EmployeeCircleProfile | None, column: str) -> str | None:
        ciphertext: str | None = getattr(record, column) if record else None
        return decrypt_pii(self._settings, ciphertext) if ciphertext else None
