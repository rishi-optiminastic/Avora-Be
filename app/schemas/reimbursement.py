"""Reimbursement request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.reimbursement import ReimbursementCategory, ReimbursementStatus
from app.schemas.common import ORMModel


class ReimbursementCreate(BaseModel):
    """An employee submits their own expense claim (amounts in minor units)."""

    amount_minor: int = Field(gt=0, le=100_000_000)  # ₹1 … ₹10,00,000
    category: ReimbursementCategory = ReimbursementCategory.OTHER
    description: str = Field(min_length=1, max_length=2000)
    expense_date: date

    @field_validator("expense_date")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("Expense date can't be in the future.")
        return value


class ReimbursementDecision(BaseModel):
    """A reviewer (manager step, then HR/Admin step) approves or rejects."""

    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class ReimbursementRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    amount_minor: int
    category: ReimbursementCategory
    description: str
    expense_date: date
    period_month: str
    status: ReimbursementStatus
    receipt_filename: str | None
    has_receipt: bool
    manager_reviewer_id: uuid.UUID | None
    manager_decided_at: datetime | None
    manager_note: str | None
    hr_reviewer_id: uuid.UUID | None
    hr_decided_at: datetime | None
    hr_note: str | None
    created_at: datetime
    updated_at: datetime
