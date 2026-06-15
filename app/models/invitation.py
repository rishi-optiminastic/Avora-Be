"""Workspace invitations.

An admin invites a person by email + role; we store only a HASH of the invite
token (the raw token travels in the emailed link, never persisted). Accepting a
valid invite provisions an employee with the invited role — the one in-PMS,
admin-authorised path that grants a role to a brand-new person (rule 5.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.employee import Role


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invitations"

    email: Mapped[str] = mapped_column(String(320), index=True)
    # Reuse the existing `role` Postgres enum — don't recreate the type.
    role: Mapped[Role] = mapped_column(Enum(Role, name="role", create_type=False))
    # Department the invitee lands in on accept (free-text, mirrors Employee.department).
    department: Mapped[str | None] = mapped_column(String(128), default=None)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[InvitationStatus] = mapped_column(default=InvitationStatus.PENDING, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    accepted_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id"), default=None
    )
