"""Attribution-correction schemas (Golden rule #5)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from app.models.attribution_correction import CorrectionStatus


class CorrectionCreate(BaseModel):
    """An employee proposes a label for their own work."""

    entity_id: uuid.UUID | None = None  # None = "not work / none"
    screenshot_id: uuid.UUID | None = None
    for_date: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=500)


class CorrectionReview(BaseModel):
    """A manager approves or rejects a proposed correction."""

    approve: bool
    review_note: str | None = Field(default=None, max_length=500)


class CorrectionRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    employee_id: uuid.UUID
    proposed_by_id: uuid.UUID
    entity_id: uuid.UUID | None
    screenshot_id: uuid.UUID | None
    for_date: str | None
    status: CorrectionStatus
    note: str | None
    reviewed_by_id: uuid.UUID | None
    review_note: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
