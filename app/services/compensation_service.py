"""Compensation business rules — the tightest authorization in the app.

Per policy (confirmed by the product owner): compensation is visible to **HR,
Admin, and the person themselves only**. Managers, senior managers, it_admin,
executives and viewers may NOT see anyone's pay — not even their reports'.
Writes are HR/Admin only. Every read and write is audited (Security rule 5.7).

Once the person is in Circle, Circle owns pay (amount, currency, period, PF)
and bank (bank name, account number, IFSC): the Circle sync keeps them in step
and a manual change here is refused with "Edit this in Circle". The target
bonus, note, effective date, account holder and account type stay Avora's.
"""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.pii_crypto import decrypt_pii, encrypt_pii
from app.models.compensation import Compensation
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.compensation import BankDetailsWrite, CompensationRead, CompensationWrite
from app.services.circle_ownership import circle_edit_url, refuse_circle_owned_changes


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

    def _account_number(self, record: Compensation | None) -> str | None:
        if record is None or not record.account_number_encrypted:
            return None
        return decrypt_pii(self._settings, record.account_number_encrypted)

    def _to_read(self, record: Compensation, employee: Employee) -> CompensationRead:
        """Decrypt the account number for the authorized viewer, then serialize."""
        circle_url = circle_edit_url(self._settings, employee) if employee.circle_linked else None
        return CompensationRead.from_model(
            record, self._account_number(record), circle_url=circle_url
        )

    async def _active_employee(self, employee_id: uuid.UUID) -> Employee:
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        return employee

    async def get(self, caller: CurrentUser, employee_id: uuid.UUID) -> CompensationRead:
        self._assert_can_view(caller, employee_id)
        record = await self._compensation.get_for_employee(employee_id)
        employee = await self._employees.get(employee_id)
        if record is None or employee is None:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.read",
            target=f"employee:{employee_id}",
        )
        return self._to_read(record, employee)

    async def set(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: CompensationWrite
    ) -> CompensationRead:
        # Writing pay is an HR/Admin action only — never the person themselves.
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._active_employee(employee_id)
        current = await self._compensation.get_for_employee(employee_id)
        refuse_circle_owned_changes(
            employee,
            _pay_of(current) if current else None,
            {
                "amount_minor": data.amount_minor,
                "currency": data.currency.upper(),
                "period": data.period,
                "pf_enabled": data.pf_enabled,
            },
            label="Pay",
        )
        record = await self._compensation.upsert(employee_id, data, updated_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.update",
            target=f"employee:{employee_id}",
        )
        return self._to_read(record, employee)

    async def set_bank(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: BankDetailsWrite
    ) -> CompensationRead:
        """Set bank details — the person themselves OR HR/Admin (self-or-manage).
        The pay amount is untouched; the account number is encrypted before it
        ever reaches the repository."""
        self._assert_can_view(caller, employee_id)  # self-or-HR/Admin
        employee = await self._active_employee(employee_id)
        current = await self._compensation.get_for_employee(employee_id)
        refuse_circle_owned_changes(
            employee,
            self._bank_of(current) if current else None,
            {
                "bank_name": data.bank_name,
                "account_number": data.account_number,
                "ifsc_code": data.ifsc_code,
            },
            label="Bank details",
        )
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
        return self._to_read(record, employee)

    def _bank_of(self, record: Compensation) -> dict[str, object]:
        return {
            "bank_name": record.bank_name,
            "account_number": self._account_number(record),
            "ifsc_code": record.ifsc_code,
        }


def _pay_of(record: Compensation) -> dict[str, object]:
    return {
        "amount_minor": record.amount_minor,
        "currency": record.currency,
        "period": record.period,
        "pf_enabled": record.pf_enabled,
    }
