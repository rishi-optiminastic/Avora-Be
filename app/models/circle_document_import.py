"""Ledger of Circle documents already copied into Avora.

One row per (employee, Circle document): it is what makes the copy happen
exactly once. It deliberately outlives the copy - if HR deletes the copied
document in Avora, the row stays (document_id becomes NULL), so the next sync
does not bring it back. Nothing here ever deletes anything in Circle.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CircleDocumentImport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "circle_document_imports"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "circle_document_id", name="uq_circle_document_imports_employee_doc"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    circle_document_id: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee_documents.id", ondelete="SET NULL"), default=None
    )
