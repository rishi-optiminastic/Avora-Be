"""Circle's pay details as a pre-fill for Avora's compensation forms.

HR/Admin/payroll-manager only (it reveals pay), enforced before Circle is ever
called, and audited. Nothing is stored: the pre-fill only fills the forms and
HR saves them through the normal endpoints. (Circle *documents* are copied by
`circle_document_sync`.)
"""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.circle import CompensationPrefill
from app.services.circle_client import CircleClient
from app.services.circle_values import amount_minor, bank_fields, parse_date, text_or_none


class CircleImportService:
    def __init__(
        self,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
        client: CircleClient,
    ) -> None:
        self._employees = employees
        self._audit = audit
        self._settings = settings
        self._client = client

    async def _employee(self, employee_id: uuid.UUID) -> Employee:
        employee = await self._employees.get(employee_id)
        if employee is None:
            raise NotFoundError()
        return employee

    async def compensation_prefill(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> CompensationPrefill:
        if not caller.can_manage_payroll:
            raise AuthorizationError()
        employee = await self._employee(employee_id)
        if not self._settings.circle_configured:
            return CompensationPrefill(found=False, configured=False)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.circle_read",
            target=f"employee:{employee_id}",
        )
        try:
            data = await self._client.compensation(employee.work_email)
        except NotFoundError:
            return CompensationPrefill(found=False)

        bank_raw = data.get("bank")
        bank, warnings = bank_fields(bank_raw if isinstance(bank_raw, dict) else {})
        ctc_text = text_or_none(data.get("annual_ctc_text"))
        amount = amount_minor(data.get("annual_ctc_inr"))
        if amount is None and ctc_text:
            warnings.append("Circle's CTC could not be read as a number; enter it manually.")
        pf_enabled = data.get("pf_enabled")
        return CompensationPrefill(
            found=True,
            employee_code=text_or_none(data.get("employee_code")),
            annual_ctc_text=ctc_text,
            amount_minor=amount,
            pf_enabled=pf_enabled if isinstance(pf_enabled, bool) else None,
            effective_date=parse_date(data.get("joining_date")),
            warnings=warnings,
            **bank,
        )
