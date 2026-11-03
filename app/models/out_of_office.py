"""Out of office — a working day spent away from the office, on company work.

Someone testing at the printing warehouse or out collecting payment is working,
but the office terminal never sees them and their laptop may be shut. Until now
the system had nothing to say about that: the day scored as absent, and the only
remedy was an HR override forcing "full day", which records neither where they
went nor who sent them.

This is deliberately NOT leave (they are working, and no quota is spent) and NOT
a regularization (nothing is being corrected, and it must not burn one of the
two monthly credits). It is its own thing: a claim about where the work happened,
approved by the manager who sent them, and visible to HR because it pays a day
with no punch behind it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OutOfOfficePortion(StrEnum):
    """How much of the day was spent away.

    HALF covers the short trip - an hour or two collecting payment - without
    building an hours tracker nobody would fill in honestly. It lines up with the
    half-day arithmetic attendance and payroll already use.
    """

    FULL_DAY = "full_day"
    HALF_DAY = "half_day"


class OutOfOfficeStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"  # withdrawn by the requester before a decision


class OutOfOffice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "out_of_office"
    __table_args__ = (
        Index("ix_out_of_office_employee_status", "employee_id", "status"),
        Index("ix_out_of_office_employee_days", "employee_id", "start_day", "end_day"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    # Local office dates (YYYY-MM-DD), like `attendance_overrides`. Attendance is
    # reckoned in office-local days, so storing an instant here would only invite
    # a timezone conversion at every read.
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))

    portion: Mapped[OutOfOfficePortion] = mapped_column(default=OutOfOfficePortion.FULL_DAY)
    # Where they went. Free text on purpose - "printing warehouse", a client's
    # name, a bank branch - because a fixed list would be wrong within a week.
    location: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(500))

    status: Mapped[OutOfOfficeStatus] = mapped_column(default=OutOfOfficeStatus.PENDING, index=True)

    # Who decided. SET NULL on offboard so the record outlives the approver.
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_note: Mapped[str | None] = mapped_column(String(500), default=None)

    def covers(self, day: str) -> bool:
        """Whether this entry spans a given local date."""
        return self.start_day <= day <= self.end_day
