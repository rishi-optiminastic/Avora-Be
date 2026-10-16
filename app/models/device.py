"""Device — one revocable credential per agent install.

Per Security rule 5.2 there is exactly one credential per device, never a shared
key baked into the binary. We store only a peppered HASH of the token. The
monotonic `last_sequence` powers replay protection on ingest (rule 5.4).

The `health_*` columns hold the LATEST capture-health report the agent sent
(POST /devices/health). They are agent claims, kept for diagnosis only; the
"needs attention" verdict is derived server-side (DeviceHealthService).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "devices"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        index=True,
    )

    # Human-facing label (hostname etc.) — not a security boundary.
    label: Mapped[str] = mapped_column(String(256))

    # Peppered HMAC-SHA256 of the device token; the raw token is shown once.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Replay protection: highest accepted per-device sequence number.
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)

    # Revocation / rotation.
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Last time we received a sample from this device (health / online status).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # --- capture health (latest agent report; all null until the first one) --- #
    health_agent_version: Mapped[str | None] = mapped_column(String(32), default=None)
    health_os: Mapped[str | None] = mapped_column(String(16), default=None)
    health_screen_capture: Mapped[str | None] = mapped_column(String(24), default=None)
    health_window_info: Mapped[str | None] = mapped_column(String(24), default=None)
    health_browser_url: Mapped[str | None] = mapped_column(String(24), default=None)
    health_last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    health_queue_items: Mapped[int | None] = mapped_column(Integer, default=None)
    health_queue_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    health_oldest_queued_age_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    # Server receive time of the latest report - never the agent's clock (rule 5.1).
    health_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Open "needs attention" incident: set on the healthy -> attention transition,
    # cleared on recovery. Makes the admin alert fire once per incident.
    health_attention_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
