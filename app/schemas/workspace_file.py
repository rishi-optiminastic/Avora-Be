"""Workspace file schemas (Golden rule #5 — never expose ORM models).

`WorkspaceFileRead` is built by the service, not `model_validate`-d from an ORM
row, because it carries joined-in `project_name` / `uploader_name` for display
(and never the file bytes).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.workspace_file import WorkspaceFileCategory


class WorkspaceFileMeta(BaseModel):
    """Metadata supplied alongside an upload (the bytes ride in the request body)."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    category: WorkspaceFileCategory = WorkspaceFileCategory.OTHER
    project_id: uuid.UUID | None = None


class WorkspaceFileRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: WorkspaceFileCategory
    content_type: str
    byte_size: int
    original_filename: str | None
    project_id: uuid.UUID | None
    project_name: str | None
    uploaded_by: uuid.UUID | None
    uploader_name: str | None
    created_at: datetime


class WorkspaceFileStats(BaseModel):
    total_files: int
    total_bytes: int
    project_count: int
    by_category: dict[str, int]
