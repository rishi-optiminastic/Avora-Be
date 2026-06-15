"""ActivitySample — raw, agent-reported activity.

Raw activity is the source of truth; rollups are derived and re-computable
(Database conventions §8). The agent is hostile, so we record BOTH the agent's
claimed timestamp and a server-stamped receive time, plus any anomaly flags.
We never order or trust by the agent clock (Security rule 5.1).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, _utcnow


class ActivitySample(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "activity_samples"
    __table_args__ = (
        # Dedup / replay key per Security rule 5.4.
        UniqueConstraint("device_id", "sequence", name="uq_activity_device_sequence"),
        Index("ix_activity_employee_received", "employee_id", "received_at"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"),
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        index=True,
    )

    # Monotonic per-device sequence (replay protection).
    sequence: Mapped[int] = mapped_column(BigInteger)

    # The agent's CLAIMED time — untrusted, kept only for skew analysis.
    client_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Server-stamped receive time — the only time we order or trust.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        index=True,
    )

    active_window: Mapped[str | None] = mapped_column(String(512), default=None)
    idle_seconds: Mapped[int] = mapped_column(default=0)

    # Anomaly flags (skew, missing heartbeat, suspiciously regular input, gaps).
    # Recorded, never used to silently drop a sample (Security rule 5.4).
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
