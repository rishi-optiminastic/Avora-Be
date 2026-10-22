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


class LeaveWithdrawalStatus(StrEnum):
    """Where an APPROVED leave's withdrawal request has got to.

    Separate from `LeaveStatus` on purpose. Until the manager decides, the leave
    is still approved and still counts against the balance and payroll - folding
    "asked to withdraw" into the leave's own status would silently return the
    days the moment someone asked for them back.
    """

    NONE = "none"  # nothing asked
    REQUESTED = "requested"  # waiting on the manager
    APPROVED = "approved"  # granted; the leave itself is now CANCELLED
    REJECTED = "rejected"  # declined; the leave stands


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

    # --- withdrawing an approved leave ------------------------------------- #
    # Taking back time off that was already granted is the manager's call, not
    # the employee's: the team planned around the absence, and after the leave
    # has started it also rewrites attendance and pay. So it is a request with
    # its own small approval cycle rather than a button.
    withdrawal_status: Mapped[LeaveWithdrawalStatus] = mapped_column(
        default=LeaveWithdrawalStatus.NONE, index=True
    )
    withdrawal_reason: Mapped[str | None] = mapped_column(String(1000), default=None)
    withdrawal_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    withdrawal_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    withdrawal_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    withdrawal_decision_note: Mapped[str | None] = mapped_column(String(1000), default=None)
