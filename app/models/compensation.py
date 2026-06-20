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
    note: Mapped[str | None] = mapped_column(String(500), default=None)

    # Who last set it — audit trail at a glance (full trail in the audit log).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
