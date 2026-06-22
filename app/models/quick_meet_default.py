"""Per-employee Quick Meet default invitees.

Each employee keeps their own list of default invitees (one row per owner). When
they click Quick Meet, everyone on their list gets a Google Calendar invite. The
list is owner-scoped — a caller only ever reads or writes their own (Golden rule
#3). Emails are stored as a JSON list (mix of colleagues and external addresses).
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class QuickMeetDefault(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "quick_meet_defaults"
    __table_args__ = (UniqueConstraint("owner_id", name="uq_quick_meet_defaults_owner"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True, nullable=False
    )
    invitee_emails: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
