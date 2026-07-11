"""Personal Access Token (PAT) for the Claude Code / API integration.

A long-lived, revocable credential an employee presents (as an
`Authorization: Bearer` header) instead of a short-lived human JWT, so a
non-browser client - the Avora MCP server that Claude Code talks to - can act as
that employee. Modelled on `EnvAccessToken`: one credential, store only a
peppered HASH, never the raw token (Security rule 5.2). The raw token is shown to
the employee exactly once at creation.

Kept as a SEPARATE table from `EnvAccessToken` on purpose: revoking a Claude Code
token must never touch Env Sync (and vice versa), and this credential carries an
optional `expires_at` that Env Sync tokens do not.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PersonalAccessToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "personal_access_tokens"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Human-facing label (e.g. "MacBook - Claude Code") - not a security boundary.
    label: Mapped[str] = mapped_column(String(256))
    # Peppered HMAC-SHA256 of the token; the raw token is shown once at creation.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Optional hard expiry. NULL means the token never expires (still revocable).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
