"""Employee document schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.document import DocumentCategory
from app.schemas.common import ORMModel


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: DocumentCategory = DocumentCategory.OTHER
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        cleaned = value.strip()
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            raise ValueError("url must be an http(s) link")
        return cleaned


class DocumentRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    title: str
    category: DocumentCategory
    url: str
    uploaded_by: uuid.UUID | None
    created_at: datetime
