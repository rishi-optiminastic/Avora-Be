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
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, _utcnow


class OcrStatus(StrEnum):
    PENDING = "pending"  # awaiting the OCR worker
    DONE = "done"
    FAILED = "failed"


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

    # Image bytes live in S3 under `object_key`; `image` is the legacy in-DB
    # fallback (used only when S3 is not configured, and for pre-S3 rows).
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    # Per-monitor rectangles within the combined capture, as [x, y, w, h] in the
    # stored image's pixels (agent-reported; multi-monitor desktops are captured
    # as one image). Empty/absent ⇒ treat the whole image as one screen. Lets the
    # OCR worker crop and OCR each monitor separately for far better accuracy.
    monitors: Mapped[list[list[int]]] = mapped_column(JSON, default=list)

    # OCR (filled by the standalone worker; used for work attribution).
    ocr_status: Mapped[OcrStatus] = mapped_column(default=OcrStatus.PENDING, index=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, default=None)

    # Structured visual context for sampled frames, cached by the EOD vision pass
    # ({apps, projects, working_on, detail}). Null until a frame is analyzed; only
    # a sampled subset is ever filled. Reused so a re-run never re-bills vision.
    vision_json: Mapped[dict[str, object] | None] = mapped_column(JSON, default=None)

    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
