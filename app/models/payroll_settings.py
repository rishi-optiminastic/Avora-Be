"""Payroll settings — the single org-wide payroll configuration (a singleton).

One row, upserted by the service (mirrors `AttendancePolicy`). Holds the pay
schedule (which day-of-month the HR digest goes out), the org currency, the
configurable recipient list, and the salary-slip knobs the calculator uses.
HR/Admin edit it; reads are HR/Admin only (the recipient list + pay schedule are
not employee-facing). Money knobs are integer minor units.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PayCycle(StrEnum):
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    WEEKLY = "weekly"


class PayrollSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payroll_settings"

    # Day of the month (1-28) the automatic HR digest is sent.
    pay_day_of_month: Mapped[int] = mapped_column(default=1)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    pay_cycle: Mapped[PayCycle] = mapped_column(default=PayCycle.MONTHLY)
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Comma-separated recipient emails (validated in the schema layer).
    recipients: Mapped[str] = mapped_column(String(2000), default="")

    # Auto-release: on `auto_release_day` (org tz), release the PREVIOUS month's
    # payslips to employees automatically — a hands-off fallback to the manual
    # "Release payslips" click, for teams whose salary lands by a fixed day.
    auto_release_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_release_day: Mapped[int] = mapped_column(default=8)

    # Salary-slip knobs (defaults reproduce the reference ₹50,000 structure).
    basic_pct: Mapped[int] = mapped_column(default=30)
    hra_pct: Mapped[int] = mapped_column(default=50)
    pf_pct: Mapped[int] = mapped_column(default=12)
    pf_cap_minor: Mapped[int] = mapped_column(BigInteger, default=180_000)
    professional_tax_minor: Mapped[int] = mapped_column(BigInteger, default=20_000)
    # ₹300 in February, ₹200 the other eleven months — the common state PT slab.
    professional_tax_feb_minor: Mapped[int] = mapped_column(BigInteger, default=30_000)
    # Withhold estimated new-regime income tax (TDS) each month.
    deduct_income_tax: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
