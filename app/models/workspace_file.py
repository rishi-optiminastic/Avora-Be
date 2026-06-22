"""Workspace file — a shared file in the team's workspace.

Unlike `EmployeeDocument` (sensitive HR records: payslips, contracts — readable
only by HR/Admin or the person), a workspace file is *deliberately* shared across
the org, like a company drive: anyone signed in can browse and upload, and a file
can be linked to a Project (work_entity) so deliverables and references live next
to the work they belong to. Delete is restricted to the uploader or HR/Admin.

Bytes live in S3 under `object_key`; `content` is the in-DB fallback used only
when S3 is not configured (mirrors `Screenshot`), so local/test runs need no AWS.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkspaceFileCategory(StrEnum):
    DELIVERABLE = "deliverable"  # shipped output for a client/project
    BRIEF = "brief"  # scope, spec, requirements
    REFERENCE = "reference"  # supporting material
    REPORT = "report"  # exported reports / analyses
    ASSET = "asset"  # design / brand / media
    OTHER = "other"


class WorkspaceFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspace_files"
    __table_args__ = (Index("ix_workspace_files_project_category", "project_id", "category"),)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    category: Mapped[WorkspaceFileCategory] = mapped_column(
        default=WorkspaceFileCategory.OTHER, index=True
    )

    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    original_filename: Mapped[str | None] = mapped_column(String(255), default=None)

    # Optional link to an admin-curated Project. SET NULL so deleting a project
    # never deletes its files (the file just becomes unfiled).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("work_entities.id", ondelete="SET NULL"), default=None, index=True
    )
    # Who uploaded it — nullable so soft-deleting the uploader never orphans rows.
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )

    # Bytes live in S3 under `object_key`; `content` is the in-DB fallback.
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
