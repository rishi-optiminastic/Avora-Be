"""Changelog entry — a product update published by an admin.

Every authenticated employee can read the changelog ("What's new"); only an
admin publishes, edits, or removes entries. Category is stored as a plain string
(validated app-side) so adding a new category never needs a DB migration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChangelogEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "changelog_entries"

    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    # feature | improvement | fix | announcement — validated by the Pydantic
    # schema on write, kept as a string here to stay migration-free as it grows.
    category: Mapped[str] = mapped_column(String(32), default="feature", index=True)
    # Optional release tag shown next to the entry, e.g. "v1.4".
    version: Mapped[str | None] = mapped_column(String(40), default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
