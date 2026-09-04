"""Reimbursement request/response schemas (Golden rule #5)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.reimbursement import ReimbursementCategory, ReimbursementStatus
from app.schemas.common import ORMModel

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


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
    # Which payroll month actually pays this out (YYYY-MM). HR only: the expense
    # month is a reasonable default but often the wrong one — an August expense
    # claimed on the 20th of September has already missed the payrun that settled
    # August, so it has to be pushed to the next open month. Omitted means "leave
    # it where it is". Ignored on the manager step and on a rejection.
    settlement_month: str | None = None

    @field_validator("settlement_month")
    @classmethod
    def _valid_settlement_month(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _MONTH_RE.match(value):
            raise ValueError("settlement_month must be YYYY-MM.")
        return value


class ReimbursementReceiptRead(ORMModel):
    """One named proof on a claim. Deliberately no bytes and no storage key — the
    file is fetched through the scoped download route, never handed out inline."""

    id: uuid.UUID
    label: str
    filename: str | None
    content_type: str
    size_bytes: int
    created_at: datetime


class SettlementMonthUpdate(BaseModel):
    """HR/finance moving an approved claim into a different payroll month."""

    settlement_month: str

    @field_validator("settlement_month")
    @classmethod
    def _valid_month(cls, value: str) -> str:
        if not _MONTH_RE.match(value):
            raise ValueError("settlement_month must be YYYY-MM.")
        return value


class ApprovalRevoke(BaseModel):
    """HR/finance taking an approved claim back out of payroll entirely."""

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
    has_receipt: bool
    receipts: list[ReimbursementReceiptRead] = Field(default_factory=list)
    manager_reviewer_id: uuid.UUID | None
    manager_decided_at: datetime | None
    manager_note: str | None
    hr_reviewer_id: uuid.UUID | None
    hr_decided_at: datetime | None
    hr_note: str | None
    created_at: datetime
    updated_at: datetime
