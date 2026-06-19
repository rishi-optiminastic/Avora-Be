"""Ping — a manager/admin "nudge" delivered down to an employee's agent.

This is the first server→agent downlink: a manager issues a ping (optional
message), the agent polls for it, plays a sound and shows the message. We keep
who issued it (audit/visibility) and stamp delivery when the agent picks it up.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, _utcnow


class PingKind(StrEnum):
    PING = "ping"  # sound + on-screen message
    CAPTURE = "capture"  # take a screenshot now (no notification)
    MODE_WORK = "mode_work"  # resume capture (employee switched to work mode)
    MODE_PERSONAL = "mode_personal"  # pause capture (employee switched to personal)


class Ping(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pings"
    __table_args__ = (Index("ix_pings_target_delivered", "target_employee_id", "delivered_at"),)

    target_employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    issued_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))

    kind: Mapped[PingKind] = mapped_column(default=PingKind.PING)
    message: Mapped[str | None] = mapped_column(String(280), default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, index=True
    )
    # Stamped when the agent first fetches it.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
