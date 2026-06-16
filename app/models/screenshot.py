"""Screenshot — periodic agent screen capture (Phase 3).

Like activity samples, the agent is hostile: we keep the agent's CLAIMED
capture time but order/trust only the server `received_at`. Image bytes are
stored in Postgres for the MVP (downscaled JPEG from the agent); retention
prunes old rows. Reads are scoped; only it_admin-excluded roles with people in
scope can view, and never out-of-scope employees.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, _utcnow


class Screenshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "screenshots"
    __table_args__ = (Index("ix_screenshots_employee_received", "employee_id", "received_at"),)

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    # Agent-claimed capture time — untrusted, kept for skew analysis only.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Server-stamped receive time — the only time we order or trust.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        index=True,
    )

    content_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    width: Mapped[int] = mapped_column(default=0)
    height: Mapped[int] = mapped_column(default=0)
    byte_size: Mapped[int] = mapped_column(default=0)
    image: Mapped[bytes] = mapped_column(LargeBinary)

    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
