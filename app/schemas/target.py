"""Target request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.models.target import TargetPeriod, TargetStatus


class TargetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    owner_id: uuid.UUID
    period: TargetPeriod = TargetPeriod.MONTHLY
    department: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    metric: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=32)
    target_value: float = Field(default=0.0, ge=0)
    start_date: datetime | None = None
    due_date: datetime | None = None
    expected_result: str | None = Field(default=None, max_length=2000)
    member_ids: list[uuid.UUID] = Field(default_factory=list)
    parent_target_id: uuid.UUID | None = None


class TargetUpdate(BaseModel):
    """All optional. The owner may set only progress fields (current_value,
    status, review_notes); the assigner may edit the rest (enforced in service)."""

    title: str | None = Field(default=None, min_length=1, max_length=256)
    owner_id: uuid.UUID | None = None
    period: TargetPeriod | None = None
    department: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    metric: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=32)
    target_value: float | None = Field(default=None, ge=0)
    current_value: float | None = Field(default=None, ge=0)
    start_date: datetime | None = None
    due_date: datetime | None = None
    expected_result: str | None = Field(default=None, max_length=2000)
    status: TargetStatus | None = None
    review_notes: str | None = Field(default=None, max_length=2000)
    member_ids: list[uuid.UUID] | None = None


class TargetRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    title: str
    description: str | None
    period: TargetPeriod
    department: str | None
    owner_id: uuid.UUID
    assigned_by_id: uuid.UUID | None
    metric: str | None
    unit: str | None
    target_value: float
    current_value: float
    start_date: datetime | None
    due_date: datetime | None
    expected_result: str | None
    status: TargetStatus
    review_notes: str | None
    member_ids: list[uuid.UUID]
    parent_target_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> int:
        if self.target_value <= 0:
            return 0
        return min(100, round((self.current_value / self.target_value) * 100))
