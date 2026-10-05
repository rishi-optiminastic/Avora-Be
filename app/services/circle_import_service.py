"""Circle data inside Avora: a compensation pre-fill and the person's documents.

Same visibility rules as Avora's own data, enforced here before Circle is ever
called:
  - Compensation pre-fill: HR/Admin/payroll-manager only (it reveals pay).
  - Documents: HR/Admin or the person themselves (as Avora's own documents).
Every read is audited. Nothing from Circle is stored: the pre-fill only fills
the forms, and documents stream straight through.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.document import DocumentCategory
from app.models.employee import Employee, Role
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.circle import CircleDocumentList, CircleDocumentRead, CompensationPrefill
from app.schemas.compensation import BankDetailsWrite
from app.services.circle_client import CircleClient, CircleFile

_PAISE_PER_RUPEE = 100

# Circle's free-text document categories (onboarding doc types) -> Avora's.
_CATEGORY_MAP: dict[str, DocumentCategory] = {
    "aadhaar card": DocumentCategory.IDENTITY,
    "pan card": DocumentCategory.IDENTITY,
    "passport photo": DocumentCategory.IDENTITY,
    "address proof": DocumentCategory.IDENTITY,
    "offer letter": DocumentCategory.CONTRACT,
    "appointment letter": DocumentCategory.CONTRACT,
    "signed offer letter": DocumentCategory.CONTRACT,
    "signed appointment letter": DocumentCategory.CONTRACT,
    "current offer letter": DocumentCategory.CONTRACT,
    "offer/appraisal letter": DocumentCategory.CONTRACT,
    "education certificates": DocumentCategory.CERTIFICATE,
    "experience letter": DocumentCategory.CERTIFICATE,
    "salary slips": DocumentCategory.PAYSLIP,
}


def map_category(circle_category: str | None) -> DocumentCategory:
    return _CATEGORY_MAP.get((circle_category or "").strip().lower(), DocumentCategory.OTHER)


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

    @staticmethod
    def _assert_can_view_documents(caller: CurrentUser, employee_id: uuid.UUID) -> None:
        # Mirrors DocumentService: HR/Admin, or the person themselves.
        if caller.role not in (Role.ADMIN, Role.HR) and caller.employee_id != employee_id:
            raise AuthorizationError()

    async def compensation_prefill(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> CompensationPrefill:
        if not caller.can_manage_payroll:
            raise AuthorizationError()
        employee = await self._employee(employee_id)
        if not self._settings.circle_configured:
            return CompensationPrefill(found=False)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="compensation.circle_read",
            target=f"employee:{employee_id}",
        )
        try:
            data = await self._client.compensation(employee.work_email)
        except NotFoundError:
            return CompensationPrefill(found=False)

        bank, warnings = _bank_fields(data.get("bank") or {})
        annual = data.get("annual_ctc_inr")
        if annual is None and data.get("annual_ctc_text"):
            warnings.append("Circle's CTC could not be read as a number; enter it manually.")
        return CompensationPrefill(
            found=True,
            employee_code=data.get("employee_code"),
            annual_ctc_text=data.get("annual_ctc_text"),
            amount_minor=annual * _PAISE_PER_RUPEE if isinstance(annual, int) else None,
            pf_enabled=data.get("pf_enabled"),
            effective_date=_parse_date(data.get("joining_date")),
            warnings=warnings,
            **bank,
        )

    async def list_documents(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> CircleDocumentList:
        self._assert_can_view_documents(caller, employee_id)
        employee = await self._employee(employee_id)
        if not self._settings.circle_configured:
            return CircleDocumentList(configured=False, documents=[])
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.circle_list",
            target=f"employee:{employee_id}",
        )
        try:
            docs = await self._client.documents(employee.work_email)
        except NotFoundError:
            docs = []
        return CircleDocumentList(
            configured=True,
            documents=[
                CircleDocumentRead(
                    id=str(d.get("id")),
                    title=str(d.get("file_name") or d.get("category") or "Document"),
                    category=map_category(d.get("category")),
                    circle_category=d.get("category"),
                    content_type=d.get("content_type"),
                    byte_size=d.get("size") if isinstance(d.get("size"), int) else None,
                    uploaded_at=d.get("uploaded_at"),
                )
                for d in docs
                if d.get("id")
            ],
        )

    async def download_document(
        self, caller: CurrentUser, employee_id: uuid.UUID, doc_id: str
    ) -> tuple[CircleFile, str]:
        """The file and a display filename. 404 (not 403) when out of scope, like
        Avora's own document downloads, so existence is not revealed."""
        try:
            self._assert_can_view_documents(caller, employee_id)
        except AuthorizationError as exc:
            raise NotFoundError() from exc
        employee = await self._employee(employee_id)
        if not self._settings.circle_configured:
            raise NotFoundError()
        listing = {str(d.get("id")): d for d in await self._client.documents(employee.work_email)}
        if doc_id not in listing:
            raise NotFoundError()
        file = await self._client.document_content(employee.work_email, doc_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.circle_download",
            target=f"employee:{employee_id}",
        )
        return file, str(listing[doc_id].get("file_name") or "document")
