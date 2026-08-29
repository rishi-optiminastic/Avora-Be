"""Payslip — an HR-released, immutable snapshot of one employee's month.

Unlike the live `estimate`/`my_slip` computation (which floats with attendance),
a row here is written only when HR **finalizes** a month: the numbers are frozen,
the slip is released to the employee, and the PDF that gets downloaded or emailed
is rendered from this snapshot. Employees can therefore only ever see months HR
has actually released. One row per (employee, month); money in integer minor
units. Identity (`employee_name`, `department`) is snapshotted too, so the slip
is self-contained and never needs a join to render.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PayslipStatus(StrEnum):
    RELEASED = "released"  # finalized by HR and visible to the employee


class Payslip(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payslips"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "period_month", name="uq_payslips_employee_id_period_month"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM

    # Identity snapshot so the slip renders without a join to `employees`.
    employee_name: Mapped[str] = mapped_column(String(256), default="")
    department: Mapped[str | None] = mapped_column(String(256), default=None)
    job_title: Mapped[str | None] = mapped_column(String(128), default=None)
    location: Mapped[str | None] = mapped_column(String(128), default=None)
    hire_date: Mapped[date | None] = mapped_column(Date, default=None)

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    monthly_ctc_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    gross_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    net_minor: Mapped[int] = mapped_column(BigInteger, default=0)  # prorated take-home
    # Approved expense claims paid out with this month's salary. Frozen here so a
    # released slip's Gross - Deductions still equals its Net years later; kept
    # out of `gross_minor`/`prorated_breakdown` because it is not taxed earnings.
    reimbursement_minor: Mapped[int] = mapped_column(BigInteger, default=0)

    # The full-month salary breakdown and its prorated counterpart (both are
    # SalaryBreakdownRead fields), frozen at release time.
    breakdown: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    prorated_breakdown: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)

    total_days: Mapped[int] = mapped_column(default=0)  # calendar days (proration base)
    working_days: Mapped[int] = mapped_column(default=0)  # weekdays minus holidays
    present_days: Mapped[float] = mapped_column(Float, default=0.0)
    paid_leave_days: Mapped[float] = mapped_column(Float, default=0.0)
    payable_days: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[PayslipStatus] = mapped_column(default=PayslipStatus.RELEASED)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
