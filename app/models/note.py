"""Note — a personal quick-capture scratchpad, private to its author.

Deliberately the simplest possible shape: no sharing, no folders, no editing —
just jot it down and it's there next time. Visibility is enforced in the
service (author-only), not here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Note(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_employee_created", "employee_id", "created_at"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(String(2000))
