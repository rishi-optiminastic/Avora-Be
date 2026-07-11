"""Notification — an in-app message delivered to one employee's inbox.

The single delivery surface for everything the system wants a person to see:
leave decisions, task assignments, and (the big one) underperformance alerts and
their escalations. Notifications are always addressed to a `recipient_id`; reads
are scoped to that recipient so nobody can read another person's inbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationKind(StrEnum):
    LEAVE_REQUEST = "leave_request"  # a report submitted leave you must review
    LEAVE_DECISION = "leave_decision"  # your leave was approved/rejected
    RESIGNATION_SUBMITTED = "resignation_submitted"  # someone resigned — HR/Admin
    RESIGNATION_DECISION = "resignation_decision"  # your resignation was decided
    TASK_ASSIGNED = "task_assigned"  # you were assigned a task
    UNDERPERFORMANCE = "underperformance"  # a person you manage is at risk
    ESCALATION = "escalation"  # a repeated issue escalated up to you
    APPRECIATION = "appreciation"  # a manager thanked you for completing a task
    SYSTEM = "system"


class NotificationLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # The inbox query: a recipient's rows, unread-first, newest-first.
        Index("ix_notifications_recipient_read", "recipient_id", "read_at"),
        # Dedupe lookups for recurring alerts (same person, kind, entity).
        Index("ix_notifications_dedupe", "recipient_id", "kind", "entity_type", "entity_id"),
    )

    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Who triggered it — null for system/automated alerts.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    kind: Mapped[NotificationKind] = mapped_column(default=NotificationKind.SYSTEM, index=True)
    level: Mapped[NotificationLevel] = mapped_column(default=NotificationLevel.INFO)

    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str | None] = mapped_column(String(500), default=None)
    # In-app destination, e.g. "/dashboard/time/leaves".
    link: Mapped[str | None] = mapped_column(String(300), default=None)

    # Optional subject of the notification — powers click-through + dedupe.
    entity_type: Mapped[str | None] = mapped_column(String(40), default=None)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
