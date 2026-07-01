"""Announcement — an org-wide banner posted by HR/Admin.

Shown in the dashboard announcement bar to every authenticated employee while
`active` and not past `expires_at`. HR/Admin author and retire them. Auto
"holiday tomorrow" notices are *derived* at read time from the holiday calendar
(see AnnouncementService) — they are not stored here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AnnouncementLevel(StrEnum):
    INFO = "info"  # neutral / violet
    SUCCESS = "success"  # good news / green
    WARNING = "warning"  # heads-up / amber
    CRITICAL = "critical"  # urgent / red


class Announcement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "announcements"

    message: Mapped[str] = mapped_column(String(500))
    level: Mapped[AnnouncementLevel] = mapped_column(default=AnnouncementLevel.INFO)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Optional auto-expiry; null = shows until HR retires it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
