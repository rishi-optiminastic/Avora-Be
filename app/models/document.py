"""Employee documents — a registry of links to externally-hosted files.

We deliberately do NOT store file bytes here (that needs a storage + encryption
decision); a document is a *reference* (title + category + URL to where the file
lives, e.g. an HR drive). Same sensitivity tier as compensation — reachable only
via its own service, which authorizes to HR/Admin or the person themselves.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentCategory(StrEnum):
    IDENTITY = "identity"
    CONTRACT = "contract"
    PAYSLIP = "payslip"
    TAX = "tax"
    CERTIFICATE = "certificate"
    OTHER = "other"


class EmployeeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employee_documents"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[DocumentCategory] = mapped_column(default=DocumentCategory.OTHER)
    url: Mapped[str] = mapped_column(String(2048))

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
