"""Regularization request/response schemas (Golden rule #5)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from app.models.regularization import RegularizationStatus

_DATE = r"^\d{4}-\d{2}-\d{2}$"


class RegularizationCreate(BaseModel):
    day: str = Field(pattern=_DATE)
    reason: str | None = Field(default=None, max_length=500)


class RegularizationReview(BaseModel):
    approve: bool
    review_note: str | None = Field(default=None, max_length=500)


class RegularizationRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    employee_id: uuid.UUID
    day: str
    reason: str | None
    status: RegularizationStatus
    reviewed_by_id: uuid.UUID | None
    review_note: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
