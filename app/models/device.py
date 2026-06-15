"""Device — one revocable credential per agent install.

Per Security rule 5.2 there is exactly one credential per device, never a shared
key baked into the binary. We store only a peppered HASH of the token. The
monotonic `last_sequence` powers replay protection on ingest (rule 5.4).
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
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
