"""Circle's pay details as a pre-fill for Avora's compensation forms.

HR/Admin/payroll-manager only (it reveals pay), enforced before Circle is ever
called, and audited. Nothing is stored: the pre-fill only fills the forms and
HR saves them through the normal endpoints. (Circle *documents* are copied by
`circle_document_sync`.)
"""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.circle import CompensationPrefill
from app.schemas.compensation import BankDetailsWrite
from app.services.circle_client import CircleClient

_PAISE_PER_RUPEE = 100
# Matches CompensationWrite's ceiling (10**15 minor units).
_MAX_ANNUAL_RUPEES = 10**13


def _bank_fields(bank: dict[str, Any]) -> tuple[dict[str, str | None], list[str]]:
    """Run Circle's bank values through Avora's own validator, one field at a
    time, so one bad value does not discard the good ones."""
    values: dict[str, str | None] = {}
    warnings: list[str] = []
    for field, label in (
        ("bank_name", "Bank name"),
        ("account_number", "Account number"),
        ("ifsc_code", "IFSC"),
    ):
        raw = bank.get(field)
        try:
            values[field] = getattr(BankDetailsWrite.model_validate({field: raw}), field)
        except PydanticValidationError:
            values[field] = None
            warnings.append(f"{label} from Circle is not valid here; enter it manually.")
    return values, warnings


def _text_or_none(raw: Any) -> str | None:
    """Circle's free-form values: numbers become text, anything else is dropped."""
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        return None
    text = str(raw).strip()
    return text or None


def _amount_minor(raw: Any) -> int | None:
    """Annual rupees (int or float, never a bool) to paise; None if unusable."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(raw) or not 0 < raw <= _MAX_ANNUAL_RUPEES:
        return None
    return round(raw * _PAISE_PER_RUPEE)


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


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
        bank, warnings = _bank_fields(bank_raw if isinstance(bank_raw, dict) else {})
        ctc_text = _text_or_none(data.get("annual_ctc_text"))
        amount_minor = _amount_minor(data.get("annual_ctc_inr"))
        if amount_minor is None and ctc_text:
            warnings.append("Circle's CTC could not be read as a number; enter it manually.")
        pf_enabled = data.get("pf_enabled")
        return CompensationPrefill(
            found=True,
            employee_code=_text_or_none(data.get("employee_code")),
            annual_ctc_text=ctc_text,
            amount_minor=amount_minor,
            pf_enabled=pf_enabled if isinstance(pf_enabled, bool) else None,
            effective_date=_parse_date(data.get("joining_date")),
            warnings=warnings,
            **bank,
        )
