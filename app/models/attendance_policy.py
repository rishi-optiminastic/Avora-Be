"""Attendance policy — the single org-wide rule set for office timings.

One row (a singleton, upserted by the service). Times are stored as minutes from
local midnight in the policy `timezone`; the classifier converts a clock-in (UTC)
into local time to compare. Admin/HR edit it; everyone may read it (so people see
the rules they're held to).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AttendancePolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attendance_policy"

    # Office hours, as minutes from local midnight (e.g. 540 = 09:00, 1080 = 18:00).
    work_start_minute: Mapped[int] = mapped_column(default=540)
    work_end_minute: Mapped[int] = mapped_column(default=1080)
    # Grace to still count on-time (e.g. 15 ⇒ on-time until 09:15).
    buffer_minutes: Mapped[int] = mapped_column(default=15)
    # Window AFTER the buffer in which a late arrival can be regularized
    # (e.g. 30 ⇒ 09:15 to 09:45 is late-but-regularizable; later ⇒ half day).
    regularization_window_minutes: Mapped[int] = mapped_column(default=30)
    # Worked-minute thresholds for the hours dimension of full/half day.
    full_day_min_minutes: Mapped[int] = mapped_column(default=480)
    half_day_min_minutes: Mapped[int] = mapped_column(default=240)
    # Regularization credits each employee gets per calendar month.
    monthly_regularizations: Mapped[int] = mapped_column(default=2)
    # Office working days per week, counted from Monday (5 ⇒ Mon-Fri, 6 ⇒ Mon-Sat).
    # The working-day baseline for payroll proration and leave-day counting.
    working_days_per_week: Mapped[int] = mapped_column(default=5)
    # Office timezone for interpreting the times above (IANA name).
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
