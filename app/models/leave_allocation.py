"""Leave allocation — a per-employee override of the org-wide leave policy.

Deliberately a SEPARATE table from `employees`/`leave_policy` (same shape as
`Compensation`): most employees use the org default, so this is a sparse,
one-row-per-overridden-employee table rather than extra columns everywhere. A
null field means "use the org policy default" for that leave type; only a
non-null value overrides it. HR/Admin only (Security rules 5.3, 5.6).
"""

from __future__ import annotations

import uuid

from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeaveAllocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_allocations"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_leave_allocations_employee_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    # Annual quota overrides, in days. Null = fall back to the org LeavePolicy.
    planned_days: Mapped[int | None] = mapped_column(Integer, default=None)
    sick_days: Mapped[int | None] = mapped_column(Integer, default=None)
    annual_days: Mapped[int | None] = mapped_column(Integer, default=None)
    bereavement_days: Mapped[int | None] = mapped_column(Integer, default=None)
    birthday_days: Mapped[int | None] = mapped_column(Integer, default=None)
    maternity_days: Mapped[int | None] = mapped_column(Integer, default=None)
    paternity_days: Mapped[int | None] = mapped_column(Integer, default=None)
    marriage_days: Mapped[int | None] = mapped_column(Integer, default=None)

    # Days of each type already taken THIS leave year before Avora held leave
    # records (Zoho, paper). Counted as used, so the card shows the real quota,
    # the real usage and the real remainder - lowering the quota instead made
    # "4 sick days, all taken" read as "no sick leave". Half days allowed.
    # Null = none entered.
    planned_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    sick_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    annual_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    bereavement_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    birthday_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    maternity_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    paternity_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    marriage_used_before: Mapped[float | None] = mapped_column(Numeric(5, 1), default=None)
    # The leave year (its 1 April) the figures above belong to, stamped when HR
    # saves them. They count only in that year, so they fall away on the next
    # 1 April by themselves instead of silently pre-spending a fresh quota.
    used_before_year_start: Mapped[date | None] = mapped_column(Date, default=None)

    note: Mapped[str | None] = mapped_column(String(500), default=None)

    # Who last set it — audit trail at a glance (full trail in the audit log).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
