"""Reimbursement — an employee expense claim, approved in two steps.

An employee submits a claim (amount + category + what it was for + expense date,
optional receipt). Their reporting MANAGER approves or rejects it first; only then
does HR/Admin give final approval. Fully-approved claims for a month feed the
payroll register (the "Reimbursement" / "Business Expense Reimbursements" columns)
and are paid with that month's salary.

The two review steps mirror the single reviewer trio used by resignations
(reviewer_id / decided_at / decision_note), duplicated once per step.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReimbursementCategory(StrEnum):
    TRAVEL = "travel"
    MEALS = "meals"
    SUPPLIES = "supplies"
    SOFTWARE = "software"
    EQUIPMENT = "equipment"
    TRAINING = "training"
    OTHER = "other"


class ReimbursementStatus(StrEnum):
    SUBMITTED = "submitted"  # awaiting the reporting manager
    MANAGER_APPROVED = "manager_approved"  # manager approved → awaiting HR/Admin
    APPROVED = "approved"  # HR/Admin approved → payable
    REJECTED = "rejected"  # declined by manager or HR (see whichever *_note is set)
    WITHDRAWN = "withdrawn"  # pulled back by the employee before final approval


class Reimbursement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reimbursements"
    __table_args__ = (
        Index("ix_reimbursements_employee_status", "employee_id", "status"),
        Index("ix_reimbursements_period_status", "period_month", "status"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[ReimbursementCategory] = mapped_column(default=ReimbursementCategory.OTHER)
    description: Mapped[str] = mapped_column(String(2000))
    expense_date: Mapped[date] = mapped_column(Date)
    # Payroll period this claim is paid in (YYYY-MM), set from the expense month on
    # submit; the payroll register reads approved claims by this key.
    period_month: Mapped[str] = mapped_column(String(7), index=True)

    # Optional receipt — S3 object key when object storage is on, else the bytes
    # live in `receipt_content` (same fallback ScreenshotService/WorkspaceFile use).
    receipt_object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    receipt_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    receipt_content_type: Mapped[str | None] = mapped_column(String(128), default=None)
    receipt_content: Mapped[bytes | None] = mapped_column(default=None)

    status: Mapped[ReimbursementStatus] = mapped_column(
        default=ReimbursementStatus.SUBMITTED, index=True
    )

    # Step 1 — reporting manager. Nullable until acted on; SET NULL if they offboard.
    manager_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    manager_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    manager_note: Mapped[str | None] = mapped_column(String(1000), default=None)

    # Step 2 — HR/Admin final approval.
    hr_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    hr_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    hr_note: Mapped[str | None] = mapped_column(String(1000), default=None)

    @property
    def has_receipt(self) -> bool:
        return bool(self.receipt_object_key or self.receipt_content)
