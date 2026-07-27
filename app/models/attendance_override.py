"""Attendance override — an HR/Admin manual correction of a day's status.

Directly forces one employee's attendance for a local office date to full-day,
half-day, or absent, regardless of what the punches/agent recorded. Applied when
attendance is computed (the day/range/monthly views) and therefore flows into the
payroll present/absent counts too. One override per (employee, day).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AttendanceOverrideStatus(StrEnum):
    FULL_DAY = "full_day"
    HALF_DAY = "half_day"
    ABSENT = "absent"


class AttendanceOverride(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attendance_overrides"
    __table_args__ = (
        UniqueConstraint("employee_id", "day", name="uq_attendance_overrides_employee_day"),
        Index("ix_attendance_overrides_employee_day", "employee_id", "day"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (local office date)
    status: Mapped[AttendanceOverrideStatus] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(500), default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
