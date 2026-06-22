"""Payroll run — a record of each HR digest that was sent.

Doubles as the auto-send idempotency guard: `period_month` is unique, so the
scheduler will not email the same month twice. A manual re-send upserts the
existing row. `created_at` (from the mixin) is the sent-at timestamp.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayrollRunSource(StrEnum):
    MANUAL = "manual"  # an HR/Admin clicked "Send to HR"
    AUTO = "auto"  # the scheduler sent it on the configured pay day


class PayrollRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("period_month", name="uq_payroll_runs_period_month"),)

    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    total_net_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    employee_count: Mapped[int] = mapped_column(default=0)
    # Snapshot of who it went to (comma-separated), for the history view.
    recipients: Mapped[str] = mapped_column(String(2000), default="")
    source: Mapped[PayrollRunSource] = mapped_column(default=PayrollRunSource.MANUAL)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
