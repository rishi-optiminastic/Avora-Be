"""Payroll adjustment — an HR/Admin manual tweak to one employee's month.

Three kinds, all applied by HR/Admin directly (no approval; audit-logged):
  - EARNING   adds a labelled amount to net pay (bonus, arrear, incentive).
  - DEDUCTION subtracts a labelled amount (advance recovery, penalty).
  - OVERRIDE  forces a computed field to a value for that month (`target` says
              which: Basic/HRA/PF/net in money, or Loss-Of-Pay in days).

The payroll estimate/register reads these by (employee, period_month) and applies
them on top of the formula-derived slip, so a month can be corrected without
changing anyone's compensation or attendance records.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayrollAdjustmentKind(StrEnum):
    EARNING = "earning"
    DEDUCTION = "deduction"
    OVERRIDE = "override"


class PayrollAdjustmentTarget(StrEnum):
    """Which computed field an OVERRIDE replaces. Money targets store the forced
    value in `amount_minor`; LOP_DAYS stores days x 100 (e.g. 2.5 days = 250)."""

    BASIC = "basic"
    HRA = "hra"
    SPECIAL_ALLOWANCE = "special_allowance"
    EMPLOYEE_PF = "employee_pf"
    LOP_DAYS = "lop_days"
    NET_PAY = "net_pay"


class PayrollAdjustment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payroll_adjustments"
    __table_args__ = (
        Index("ix_payroll_adjustments_employee_period", "employee_id", "period_month"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    kind: Mapped[PayrollAdjustmentKind] = mapped_column()
    label: Mapped[str] = mapped_column(String(200))
    # EARNING/DEDUCTION: the money moved. OVERRIDE: the forced value (money in
    # minor units, or days x 100 when target is LOP_DAYS).
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    # Only set (and required) when kind is OVERRIDE.
    target: Mapped[PayrollAdjustmentTarget | None] = mapped_column(default=None)
    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
