"""Payroll receipt - the evidence that a month's payroll was actually paid.

Distinct from `PayoutUpload`, which is *data*: the bank file, parsed into rows
and reconciled line by line against what Avora said was owed. A receipt is the
*document* - the bank's transfer advice, a UTR confirmation, a stamped
statement - the person who ran payroll keeps as proof the money left. One is
for finding mistakes; this one is for showing someone later that the run
happened, which is what an audit, a dispute or a departing employee asks for.

Several per month is normal and allowed: salaries and reimbursements often go
out as separate transfers, and a bank will issue an advice per batch.

Bytes live in S3 under `object_key`, with `content` as the in-DB fallback used
only when S3 is not configured - the same arrangement `WorkspaceFile` and
`Screenshot` use, so local and test runs need no AWS.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayrollReceiptKind(StrEnum):
    BANK_ADVICE = "bank_advice"  # the bank's transfer advice for the batch
    UTR_CONFIRMATION = "utr_confirmation"  # per-transfer reference confirmation
    BANK_STATEMENT = "bank_statement"  # the statement line showing the debit
    OTHER = "other"


class PayrollReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payroll_receipts"
    __table_args__ = (Index("ix_payroll_receipts_month_kind", "period_month", "kind"),)

    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    kind: Mapped[PayrollReceiptKind] = mapped_column(default=PayrollReceiptKind.BANK_ADVICE)

    # What the document says, typed in by the uploader so the month can be
    # answered for without opening every attachment.
    reference: Mapped[str | None] = mapped_column(String(64), default=None)  # UTR / batch ref
    paid_on: Mapped[date | None] = mapped_column(Date, default=None)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    original_filename: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    # Lets two uploads of the same document be recognised as the same document.
    file_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
