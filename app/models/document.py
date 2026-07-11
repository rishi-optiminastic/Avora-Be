"""Employee documents — an HR/Admin registry of an employee's files.

A document is either a *link* (`url` set — a reference to where the file lives,
e.g. an HR drive) or an *uploaded file* (bytes in S3 under `object_key`, with an
in-DB `content` fallback — the same storage shape as workspace files/screenshots).
Same sensitivity tier as compensation — reachable only via its own service, which
authorizes to HR/Admin (writes) or the person themselves (reads).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
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
    # Set for a link document (reference to an external file). Null for an upload.
    url: Mapped[str | None] = mapped_column(String(2048), default=None)

    # Uploaded-file fields (all null for a link). Bytes live in S3 under
    # `object_key`; `content` is the in-DB fallback when S3 is not configured.
    content_type: Mapped[str | None] = mapped_column(String(128), default=None)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    original_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    @property
    def is_link(self) -> bool:
        return self.url is not None
