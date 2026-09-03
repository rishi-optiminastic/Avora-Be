"""Compensation — the most sensitive employee data we hold.

Deliberately a SEPARATE table from `employees` (not extra columns) so it can
never leak through `EmployeeRead`: it is only ever reachable via its own
service, which authorizes to HR/Admin or the person themselves (Security rules
5.3, 5.6). One current record per employee; amounts are stored in integer minor
units (e.g. cents) to avoid floating-point money.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import BigInteger, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayPeriod(StrEnum):
    ANNUAL = "annual"
    MONTHLY = "monthly"


class AccountType(StrEnum):
    SAVINGS = "savings"
    CURRENT = "current"


class Compensation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compensations"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_compensations_employee_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    # Base pay and (optional) target bonus, in integer minor units of `currency`.
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    bonus_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    period: Mapped[PayPeriod] = mapped_column(default=PayPeriod.ANNUAL)
    effective_date: Mapped[date | None] = mapped_column(Date, default=None)
    # Whether Provident Fund applies to this person. PF sits on BOTH sides of the
    # slip — the employer share is carved out of CTC to reach gross, and the
    # employee share is deducted from it — so switching it off raises take-home
    # by roughly twice the PF amount. Per-employee because whether someone is
    # covered is a fact about their contract (consultants, and staff above the
    # wage ceiling who opted out), not an org-wide policy. Defaults to True so
    # every existing record keeps deducting PF exactly as it does today.
    pf_enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    note: Mapped[str | None] = mapped_column(String(500), default=None)

    # Bank details for salary disbursal. The account number is the sensitive PII,
    # stored as Fernet ciphertext (Security rule 5.6); the rest are low-risk (a
    # name / a public IFSC code) and kept in plaintext for querying/display.
    account_holder_name: Mapped[str | None] = mapped_column(String(128), default=None)
    bank_name: Mapped[str | None] = mapped_column(String(128), default=None)
    account_number_encrypted: Mapped[str | None] = mapped_column(String(512), default=None)
    ifsc_code: Mapped[str | None] = mapped_column(String(16), default=None)
    account_type: Mapped[AccountType | None] = mapped_column(default=None)
    # How salary is paid, for the payroll register (e.g. "Direct Deposit",
    # "Manual Bank Transfer"). Free-form; HR-set. Defaults to a bank transfer.
    payment_mode: Mapped[str] = mapped_column(String(64), default="Bank Transfer")

    # Who last set it — audit trail at a glance (full trail in the audit log).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
