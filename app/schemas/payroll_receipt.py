"""Payroll receipt schemas - proof that a month's payroll was actually paid."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.payroll_receipt import PayrollReceipt, PayrollReceiptKind
from app.schemas.common import ORMModel


class PayrollReceiptCreate(BaseModel):
    """What the person who ran payroll types in alongside the document.

    Everything but the month is optional: the point is to get the evidence
    filed, and refusing it because a UTR is not to hand is how the evidence
    stops being filed at all.
    """

    period_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    kind: PayrollReceiptKind = PayrollReceiptKind.BANK_ADVICE
    reference: str | None = Field(default=None, max_length=64)
    paid_on: date | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=1000)


class PayrollReceiptRead(ORMModel):
    id: uuid.UUID
    period_month: str
    kind: PayrollReceiptKind
    reference: str | None
    paid_on: date | None
    amount_minor: int | None
    currency: str
    note: str | None
    original_filename: str
    content_type: str
    byte_size: int
    uploaded_by: uuid.UUID | None
    uploaded_by_name: str | None = None
    created_at: datetime

    @classmethod
    def from_model(
        cls, model: PayrollReceipt, uploader_name: str | None = None
    ) -> PayrollReceiptRead:
        return cls(
            id=model.id,
            period_month=model.period_month,
            kind=model.kind,
            reference=model.reference,
            paid_on=model.paid_on,
            amount_minor=model.amount_minor,
            currency=model.currency,
            note=model.note,
            original_filename=model.original_filename,
            content_type=model.content_type,
            byte_size=model.byte_size,
            uploaded_by=model.uploaded_by,
            uploaded_by_name=uploader_name,
            created_at=model.created_at,
        )


class PayrollProofStatus(BaseModel):
    """Whether a month can show that it was paid.

    Read by the payroll panel so an unevidenced month is visible as such rather
    than merely absent from a list nobody opens.
    """

    period_month: str
    receipt_count: int
    total_minor: int
    currency: str
    latest_paid_on: date | None
    # True once there is at least one receipt. Named for the question being
    # asked ("can we prove this?") rather than the row count behind it.
    has_proof: bool
