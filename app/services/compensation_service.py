"""Compensation business rules — the tightest authorization in the app.

Per policy (confirmed by the product owner): compensation is visible to **HR,
Admin, and the person themselves only**. Managers, senior managers, it_admin,
executives and viewers may NOT see anyone's pay — not even their reports'.
Writes are HR/Admin only. Every read and write is audited (Security rule 5.7).
"""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.pii_crypto import decrypt_pii, encrypt_pii
from app.models.compensation import Compensation
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.compensation import BankDetailsWrite, CompensationRead, CompensationWrite


def _can_manage(caller: CurrentUser) -> bool:
    """HR/Admin (or a payroll-manager grant holder) may read and write anyone's
    compensation — needed for salary processing."""
    return caller.can_manage_payroll


class CompensationService:
    def __init__(
        self,
        compensation: CompensationRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._compensation = compensation
        self._employees = employees
        self._audit = audit
        self._settings = settings

    def _assert_can_view(self, caller: CurrentUser, employee_id: uuid.UUID) -> None:
        # HR/Admin see all; everyone else only their own. No manager carve-out.
        if not _can_manage(caller) and caller.employee_id != employee_id:
            raise AuthorizationError()

    def _to_read(self, record: Compensation) -> CompensationRead:
        """Decrypt the account number for the authorized viewer, then serialize."""
        account_number = (
            decrypt_pii(self._settings, record.account_number_encrypted)
            if record.account_number_encrypted
            else None
        )
        return CompensationRead.from_model(record, account_number)

    async def get(self, caller: CurrentUser, employee_id: uuid.UUID) -> CompensationRead:
        self._assert_can_view(caller, employee_id)
        record = await self._compensation.get_for_employee(employee_id)
        if record is None:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.read",
            target=f"employee:{employee_id}",
        )
        return self._to_read(record)

    async def set(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: CompensationWrite
    ) -> CompensationRead:
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
        return self._to_read(record)

    async def set_bank(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: BankDetailsWrite
    ) -> CompensationRead:
        """Set bank details — the person themselves OR HR/Admin (self-or-manage).
        The pay amount is untouched; the account number is encrypted before it
        ever reaches the repository."""
        self._assert_can_view(caller, employee_id)  # self-or-HR/Admin
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        encrypted = (
            encrypt_pii(self._settings, data.account_number) if data.account_number else None
        )
        record = await self._compensation.upsert_bank(
            employee_id,
            account_holder_name=data.account_holder_name,
            bank_name=data.bank_name,
            account_number_encrypted=encrypted,
            ifsc_code=data.ifsc_code,
            account_type=data.account_type,
            updated_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.bank.update",
            target=f"employee:{employee_id}",
        )
        return self._to_read(record)
