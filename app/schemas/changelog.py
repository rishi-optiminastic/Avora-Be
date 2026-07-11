"""Changelog request/response schemas.

`ChangelogRead` is the published shape every employee sees. Create/Update are
admin-only inputs (authorization lives in the service). Category is a closed set
validated here; the column stays a plain string so the set can grow freely.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class ChangelogCategory(StrEnum):
    FEATURE = "feature"  # brand-new capability
    IMPROVEMENT = "improvement"  # enhancement to something that exists
    FIX = "fix"  # bug fix
    ANNOUNCEMENT = "announcement"  # general product news


def _stripped(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Cannot be empty.")
    return cleaned


class ChangelogCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    category: ChangelogCategory = ChangelogCategory.FEATURE
    version: str | None = Field(default=None, max_length=40)

    @field_validator("title", "body")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _stripped(value)

    @field_validator("version")
    @classmethod
    def _strip_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ChangelogUpdate(BaseModel):
    """PATCH — only the supplied fields change."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=8000)
    category: ChangelogCategory | None = None
    version: str | None = Field(default=None, max_length=40)

    @field_validator("title", "body")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return None if value is None else _stripped(value)

    @field_validator("version")
    @classmethod
    def _strip_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ChangelogRead(ORMModel):
    id: uuid.UUID
    title: str
    body: str
    category: str
    version: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
