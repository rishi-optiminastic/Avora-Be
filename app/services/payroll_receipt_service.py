"""Proof that a month's payroll was actually paid.

The payroll run tells you what *should* have gone out. This tells you that it
did: the bank's advice, a UTR confirmation, the statement line. The people who
can run payroll are the people who can file and read it (`can_manage_payroll`)
- it names bank references and amounts, so it is not ordinary workspace
reading.

Nothing here blocks payroll. A month with no receipt is reported as unevidenced
(`PayrollProofStatus.has_proof`) and the panel asks for one, rather than the
finalize button refusing: withholding people's payslips because a PDF has not
been filed yet would punish the wrong person for the wrong thing.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.payroll_receipt import PayrollReceipt
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.payroll_receipt import PayrollReceiptRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.schemas.auth import CurrentUser
from app.schemas.payroll_receipt import (
    PayrollProofStatus,
    PayrollReceiptCreate,
    PayrollReceiptRead,
)

logger = get_logger("app.payroll.receipt")

# A receipt is a document, not a photo album. PDFs cover almost every bank, and
# a screenshot of a confirmation screen is a legitimate second-best.
ALLOWED_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
MAX_BYTES = 15 * 1024 * 1024


class PayrollReceiptService:
    def __init__(
        self,
        settings: Settings,
        receipts: PayrollReceiptRepository,
        payroll_settings: PayrollSettingsRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._settings = settings
        self._receipts = receipts
        self._payroll_settings = payroll_settings
        self._employees = employees
        self._audit = audit

    async def _currency(self) -> str:
        """The org's payroll currency, from payroll settings - the same source
        the register and the payslips use, so a receipt can never be filed in a
        currency the month was not paid in."""
        row = await self._payroll_settings.get()
        return row.currency if row is not None else "INR"

    @staticmethod
    def _guard(caller: CurrentUser) -> None:
        """Whoever may run payroll may evidence it, and nobody else.

        Deliberately the same gate as the rest of the payroll cluster rather
        than a looser one: a transfer advice lists the payroll total and the
        company's own bank references.
        """
        if not caller.can_manage_payroll:
            raise AuthorizationError("Only payroll managers can work with payroll receipts.")

    async def add(
        self,
        caller: CurrentUser,
        meta: PayrollReceiptCreate,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> PayrollReceiptRead:
        self._guard(caller)
        if not data:
            raise ValidationError("The receipt file is empty.")
        if len(data) > MAX_BYTES:
            raise ValidationError("Receipt is too large (max 15 MB).")
        if content_type not in ALLOWED_TYPES:
            raise ValidationError("A receipt must be a PDF or an image.")

        digest = hashlib.sha256(data).hexdigest()
        if await self._receipts.find_duplicate(meta.period_month, digest):
            # The same bytes against the same month is a double-click or a
            # re-upload, never a second payment. Counting it twice would
            # overstate what the month can prove.
            raise ConflictError("That receipt is already filed against this month.")

        receipt = PayrollReceipt(
            period_month=meta.period_month,
            kind=meta.kind,
            reference=(meta.reference or None),
            paid_on=meta.paid_on,
            amount_minor=meta.amount_minor,
            currency=await self._currency(),
            note=(meta.note or None),
            original_filename=filename[:255],
            content_type=content_type,
            byte_size=len(data),
            file_sha256=digest,
            uploaded_by=caller.employee_id,
        )
        receipt.object_key, receipt.content = await self._store(data, content_type, filename)

        self._receipts.add(receipt)
        await self._receipts.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.receipt.add",
            target=f"month:{meta.period_month}:receipt:{receipt.id}",
        )
        return PayrollReceiptRead.from_model(receipt, await self._name_of(caller.employee_id))

    async def _store(
        self, data: bytes, content_type: str, filename: str
    ) -> tuple[str | None, bytes | None]:
        """S3 when configured, the row otherwise - the same fallback workspace
        files use, so local and test runs need no AWS."""
        if not self._settings.s3_enabled:
            return None, data
        key = storage.workspace_object_key(uuid.uuid4().hex, filename)
        try:
            await storage.put_object(key, data, content_type)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("payroll_receipt_s3_put_failed", extra={"key": key})
            raise StorageError() from exc
        return key, None

    async def list_for_month(
        self, caller: CurrentUser, period_month: str
    ) -> list[PayrollReceiptRead]:
        self._guard(caller)
        rows = await self._receipts.list_for_month(period_month)
        names = await self._uploader_names(rows)
        return [PayrollReceiptRead.from_model(r, names.get(r.uploaded_by)) for r in rows]

    async def proof_status(self, caller: CurrentUser, period_month: str) -> PayrollProofStatus:
        """Whether this month can be shown to have been paid, and for how much.

        `total_minor` sums only the receipts whose amount was filled in, so it
        is "what the evidence accounts for", not a second opinion on payroll.
        """
        self._guard(caller)
        rows = await self._receipts.list_for_month(period_month)
        dates = [r.paid_on for r in rows if r.paid_on is not None]
        return PayrollProofStatus(
            period_month=period_month,
            receipt_count=len(rows),
            total_minor=sum(r.amount_minor or 0 for r in rows),
            currency=await self._currency(),
            latest_paid_on=max(dates) if dates else None,
            has_proof=bool(rows),
        )

    async def file_bytes(
        self, caller: CurrentUser, receipt_id: uuid.UUID
    ) -> tuple[bytes, str, str]:
        """The document itself. Audited: evidence leaving the system is exactly
        the kind of read the trail exists for (rule 5.7)."""
        self._guard(caller)
        receipt = await self._receipts.get(receipt_id)
        if receipt is None:
            raise NotFoundError()
        data = receipt.content
        if data is None and receipt.object_key:
            try:
                data = await storage.get_object(receipt.object_key)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("payroll_receipt_s3_get_failed", extra={"key": receipt.object_key})
                raise StorageError() from exc
        if data is None:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.receipt.download",
            target=f"receipt:{receipt.id}",
        )
        return data, receipt.content_type, receipt.original_filename or "receipt"

    async def remove(self, caller: CurrentUser, receipt_id: uuid.UUID) -> None:
        """Filed the wrong document, or against the wrong month.

        The row goes, but the audit entry stays - the trail is append-only
        (rule 5.7), so removing evidence is itself recorded.
        """
        self._guard(caller)
        receipt = await self._receipts.get(receipt_id)
        if receipt is None:
            raise NotFoundError()
        key, month = receipt.object_key, receipt.period_month
        await self._receipts.delete(receipt)
        await self._receipts.flush()
        if key and self._settings.s3_enabled:
            try:
                await storage.delete_objects([key])
            except (ClientError, BotoCoreError):
                # The row is gone either way; an orphaned object is cheaper
                # than failing a delete the user already saw succeed.
                logger.warning("payroll_receipt_s3_delete_failed", extra={"key": key})
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.receipt.remove",
            target=f"month:{month}:receipt:{receipt_id}",
        )

    async def _name_of(self, employee_id: uuid.UUID | None) -> str | None:
        if employee_id is None:
            return None
        employee = await self._employees.get(employee_id)
        return employee.full_name if employee is not None else None

    async def _uploader_names(
        self, rows: Sequence[PayrollReceipt]
    ) -> dict[uuid.UUID | None, str | None]:
        ids = [r.uploaded_by for r in rows if r.uploaded_by is not None]
        if not ids:
            return {}
        found = await self._employees.get_many(ids)
        return {eid: person.full_name for eid, person in found.items()}
