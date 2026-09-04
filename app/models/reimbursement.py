"""Reimbursement — an employee expense claim, approved in two steps.

An employee submits a claim (amount + category + what it was for + expense date,
any number of named proofs). Their reporting MANAGER approves or rejects it first; only then
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    # Proofs live in their own table — a claim usually needs more than one document
    # (invoice + toll slip + boarding pass), each named by the person submitting so
    # a reviewer can tell them apart without opening every file. Eagerly loaded:
    # every list view shows how many proofs a claim carries.
    receipts: Mapped[list[ReimbursementReceipt]] = relationship(
        back_populates="reimbursement",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ReimbursementReceipt.created_at",
    )

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
        return bool(self.receipts)


class ReimbursementReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One named proof attached to a claim (invoice, bill, boarding pass…).

    Bytes go to S3 when object storage is on — only `object_key` is stored — else
    they live in `content`, the same fallback screenshots and workspace files use.
    Deleting the claim deletes its proofs (cascade both in the ORM and the FK).
    """

    __tablename__ = "reimbursement_receipts"

    reimbursement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reimbursements.id", ondelete="CASCADE"), index=True
    )
    # What the claimant calls this document. Defaulted from the filename when they
    # do not type one, so a proof is never nameless in the reviewer's list.
    label: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str | None] = mapped_column(String(255), default=None)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content: Mapped[bytes | None] = mapped_column(default=None)

    reimbursement: Mapped[Reimbursement] = relationship(back_populates="receipts")
