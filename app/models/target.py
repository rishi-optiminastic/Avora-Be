"""Target — a monthly/quarterly goal assigned to a manager.

Admins and senior managers assign targets to a manager (the owner); the owner
tracks progress (actual vs planned) until the deadline. A quarterly target can be
broken into monthly milestones via `parent_target_id`. Reads are scoped to the
caller exactly like tasks (Security rule 5.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TargetPeriod(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class TargetStatus(StrEnum):
    PLANNED = "planned"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    ACHIEVED = "achieved"
    MISSED = "missed"


class Target(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "targets"
    __table_args__ = (Index("ix_targets_owner_status", "owner_id", "status"),)

    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(String(2000), default=None)

    period: Mapped[TargetPeriod] = mapped_column(default=TargetPeriod.MONTHLY, index=True)
    department: Mapped[str | None] = mapped_column(String(128), default=None, index=True)

    # The manager who owns/delivers it; who assigned it (admin/senior manager).
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    metric: Mapped[str | None] = mapped_column(String(128), default=None)  # e.g. "Revenue"
    unit: Mapped[str | None] = mapped_column(String(32), default=None)  # e.g. "₹", "leads"
    target_value: Mapped[float] = mapped_column(Float, default=0.0)  # planned
    current_value: Mapped[float] = mapped_column(Float, default=0.0)  # actual

    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    expected_result: Mapped[str | None] = mapped_column(String(2000), default=None)
    status: Mapped[TargetStatus] = mapped_column(default=TargetStatus.PLANNED, index=True)
    review_notes: Mapped[str | None] = mapped_column(String(2000), default=None)

    # Team members involved (employee ids) + quarterly→monthly milestone link.
    member_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent_target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("targets.id", ondelete="SET NULL"), default=None, index=True
    )
