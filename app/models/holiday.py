"""Holiday — the organisation-wide holiday calendar.

Visible to every authenticated employee; only HR/admin may add, update, or
remove entries (Security rule 5.3 — write authority enforced in the service).
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class HolidayType(StrEnum):
    PUBLIC = "public"
    OPTIONAL = "optional"
    COMPANY = "company"


class Holiday(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "holidays"

    name: Mapped[str] = mapped_column(String(256))
    # Calendar date (no time component) — the org is closed this whole day.
    date: Mapped[date] = mapped_column(Date, index=True)
    holiday_type: Mapped[HolidayType] = mapped_column(default=HolidayType.PUBLIC, index=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
