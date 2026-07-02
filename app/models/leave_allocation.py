"""Leave allocation — a per-employee override of the org-wide leave policy.

Deliberately a SEPARATE table from `employees`/`leave_policy` (same shape as
`Compensation`): most employees use the org default, so this is a sparse,
one-row-per-overridden-employee table rather than extra columns everywhere. A
null field means "use the org policy default" for that leave type; only a
non-null value overrides it. HR/Admin only (Security rules 5.3, 5.6).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
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
    note: Mapped[str | None] = mapped_column(String(500), default=None)

    # Who last set it — audit trail at a glance (full trail in the audit log).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
