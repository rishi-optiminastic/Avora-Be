"""Resignation request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.resignation import ResignationStatus
from app.schemas.common import ORMModel


class ResignationCreate(BaseModel):
    """An employee submits their own resignation."""

    reason: str | None = Field(default=None, max_length=2000)
    last_working_day: date

    @field_validator("last_working_day")
    @classmethod
    def _not_in_past(cls, value: date) -> date:
        if value < datetime.now(UTC).date():
            raise ValueError("Last working day can't be in the past.")
        return value


class ResignationDecision(BaseModel):
    """HR/Admin accept or reject a submitted resignation."""

    accept: bool
    note: str | None = Field(default=None, max_length=1000)


class ResignationRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    reason: str | None
    last_working_day: date
    status: ResignationStatus
    reviewer_id: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
    updated_at: datetime
