"""Env Sync Personal Access Token (PAT).

A long-lived, revocable credential the VSCode extension presents instead of a
short-lived human JWT. Modelled on `Device`: one credential, store only a
peppered HASH, never the raw token (Security rule 5.2). The raw token is shown
to the employee exactly once at creation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EnvAccessToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "env_access_tokens"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Human-facing label (e.g. "MacBook · VSCode") — not a security boundary.
    label: Mapped[str] = mapped_column(String(256))
    # Peppered HMAC-SHA256 of the token; the raw token is shown once at creation.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
