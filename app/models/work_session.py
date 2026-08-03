"""Work session — an explicit clock-in/clock-out for the work day.

Separate from tracking mode (work/personal) and from the activity-derived
attendance: this is the employee saying "I've started work" in the morning and
"I'm leaving" in the evening. One session is open (clock_out_at is NULL) at a
time. The manager attendance view prefers these explicit times over the times
inferred from activity samples when a session exists for the day.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, _utcnow


class WorkSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_sessions"
    __table_args__ = (Index("ix_work_sessions_employee_clock_in", "employee_id", "clock_in_at"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    clock_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # How the session was STARTED (dashboard, biometric, agent, …) — for audit/visibility.
    source: Mapped[str] = mapped_column(String(32), default="dashboard")
    # How it was ENDED: dashboard (the person clicked it), biometric (out-punch),
    # auto (the auto-checkout worker closed a forgotten session). NULL while open.
    clock_out_source: Mapped[str | None] = mapped_column(String(32), default=None)
    # Server-observed at clock-in (spec: login IP + location for HR/attendance).
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    # `location` holds the matched office name when a geofenced clock-in passes.
    location: Mapped[str | None] = mapped_column(String(128), default=None)
    # GPS reported by the browser at clock-in (a claim — stored for the record).
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
