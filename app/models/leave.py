"""Leave — time-off requests and their approval workflow.

A leave is requested BY an employee and decided BY their reviewer (manager / HR /
admin). Reads are scoped to the caller like employees/tasks: own requests, your
reports'/department's, or everything (admin/HR), plus anything you review.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeaveType(StrEnum):
    PLANNED = "planned"
    ANNUAL = "annual"
    SICK = "sick"
    BEREAVEMENT = "bereavement"
    BIRTHDAY = "birthday"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    MARRIAGE = "marriage"
    UNPAID = "unpaid"
    HALF_DAY = "half_day"


class HalfDayPeriod(StrEnum):
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"


class LeaveStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    WITHDRAWN = "withdrawn"


class Leave(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leaves"
    __table_args__ = (Index("ix_leaves_employee_status", "employee_id", "status"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    leave_type: Mapped[LeaveType] = mapped_column(default=LeaveType.PLANNED, index=True)
    # Only set (and meaningful) when leave_type is HALF_DAY — which half was taken.
    half_day_period: Mapped[HalfDayPeriod | None] = mapped_column(default=None)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(1000), default=None)

    status: Mapped[LeaveStatus] = mapped_column(default=LeaveStatus.SUBMITTED, index=True)

    # Who decided (manager/HR/admin) — nullable until decided, SET NULL on offboard.
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decision_note: Mapped[str | None] = mapped_column(String(1000), default=None)
