"""Payout verification - the accounts team's bank payout file, checked against payroll.

After a month is paid, accounts uploads the file they paid from. Each upload is
kept (`PayoutUpload`) with every payment line in it (`PayoutRow`), and the
comparison against what Avora says should have been paid is stored as
`PayoutMismatch` rows HR can work through and resolve with a note.

Money is integer minor units. Bank account numbers are never stored in full:
a row keeps only the last four digits, enough to recognise the line.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayoutRunKind(StrEnum):
    """Which of payroll's exports the payout file was paid from.

    The salary register folds approved reimbursements into Net Pay by default;
    when expenses are paid from their own run, the salary file leaves them out
    and the reimbursement register carries only them. Each kind expects exactly
    what its export put on the row.
    """

    SALARY = "salary"  # /payroll/export (reimbursements folded into Net Pay)
    SALARY_ONLY = "salary_only"  # /payroll/export?include_reimbursements=false
    REIMBURSEMENTS = "reimbursements"  # /payroll/export/reimbursements


class PayoutRowStatus(StrEnum):
    MATCHED = "matched"  # tied to an employee
    UNMATCHED = "unmatched"  # no employee number, account or email we know
    FAILED = "failed"  # the bank reported the transfer failed; not counted as paid


class PayoutMismatchKind(StrEnum):
    PAID_NOT_EXPECTED = "paid_not_expected"
    EXPECTED_NOT_PAID = "expected_not_paid"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE_PAYMENT = "duplicate_payment"
    UNMATCHED_ROW = "unmatched_row"
    # Tied to a person by employee number or email, but paid into a bank account
    # other than the one on file (leading zeros ignored).
    ACCOUNT_MISMATCH = "account_mismatch"


class PayoutUpload(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payout_uploads"

    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    kind: Mapped[PayoutRunKind] = mapped_column(default=PayoutRunKind.SALARY)
    filename: Mapped[str] = mapped_column(String(255), default="")
    file_type: Mapped[str] = mapped_column(String(8), default="")
    file_sha256: Mapped[str] = mapped_column(String(64), default="")
    # Comma-separated identifier columns the file offered (employee_number, ...).
    identifier_columns: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    row_count: Mapped[int] = mapped_column(default=0)
    matched_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    expected_count: Mapped[int] = mapped_column(default=0)
    expected_total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    paid_total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    mismatch_count: Mapped[int] = mapped_column(default=0)
    # Set once HR/payroll managers were flagged, so an upload never alerts twice.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PayoutRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payout_rows"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payout_uploads.id", ondelete="CASCADE"), index=True
    )
    row_number: Mapped[int] = mapped_column(default=0)  # as seen in the uploaded sheet
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    matched_by: Mapped[str | None] = mapped_column(String(16), default=None)
    employee_number: Mapped[str | None] = mapped_column(String(64), default=None)
    account_last4: Mapped[str | None] = mapped_column(String(4), default=None)
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    payee_name: Mapped[str | None] = mapped_column(String(256), default=None)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    reference: Mapped[str | None] = mapped_column(String(64), default=None)  # UTR
    bank_status: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[PayoutRowStatus] = mapped_column(default=PayoutRowStatus.UNMATCHED)


class PayoutMismatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payout_mismatches"

    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payout_uploads.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[PayoutMismatchKind] = mapped_column()
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payout_rows.id", ondelete="CASCADE"), default=None
    )
    expected_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    paid_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    resolution_note: Mapped[str | None] = mapped_column(String(1000), default=None)
