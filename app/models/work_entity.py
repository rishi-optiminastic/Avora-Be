"""WorkEntity — an admin-curated "thing people work on" (project/account/
campaign/workbook/…) used for work attribution.

Each entity carries a small text profile (keywords + domains) that we match
against a person's on-screen text (OCR), current app, and current domain to
infer what they're working on. Admin-managed (Security rule 5.3 — writes
enforced in the service).
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkEntity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_entities"

    name: Mapped[str] = mapped_column(String(256))
    # Free-text department (matches Employee.department); None = org-wide.
    department: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    description: Mapped[str | None] = mapped_column(String(1024), default=None)

    # Match profile. keywords/aliases/repos/clients/tool-names all go in keywords;
    # domains are matched against the person's current browsing domain.
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    domains: Mapped[list[str]] = mapped_column(JSON, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
