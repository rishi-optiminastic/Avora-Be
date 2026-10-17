"""Workbook - a spreadsheet kept in Avora (the Sheets page).

`snapshot` is the editor's whole workbook as JSON (Univer's IWorkbookData):
cells, styles, merges, data validation, every tab. Avora stores it as an opaque
document; the only things the server reads are its size and its links (see
`app.core.workbook_snapshot`).

Editing is one person at a time with optimistic locking: every save carries the
`version` it started from and is refused (409) when someone saved since, so a
save can never silently overwrite another person's work.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkbookSharing(StrEnum):
    PRIVATE = "private"  # the owner (and admins) only
    ORG_VIEW = "org_view"  # everyone may open it; the owner edits
    ORG_EDIT = "org_edit"  # everyone may open and edit it


class Workbook(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workbooks"
    __table_args__ = (Index("ix_workbooks_sharing_updated", "sharing", "updated_at"),)

    title: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    sharing: Mapped[WorkbookSharing] = mapped_column(default=WorkbookSharing.PRIVATE)
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    version: Mapped[int] = mapped_column(default=1)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
