"""Work-entity request/response schemas (Golden rule #5)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field


class WorkEntityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    department: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    keywords: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class WorkEntityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    department: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    keywords: list[str] | None = None
    domains: list[str] | None = None
    is_active: bool | None = None


class WorkEntityRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    department: str | None
    description: str | None
    keywords: list[str]
    domains: list[str]
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime


class WorkEntityOption(BaseModel):
    """Minimal project shape for a task's project picker (managers); the
    attribution keywords/domains are not exposed here."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    department: str | None
