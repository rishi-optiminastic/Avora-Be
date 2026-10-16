"""Payout verification request/response schemas (Golden rule #5).

Money is integer minor units. A payout line's bank account is only ever shown
as its last four digits.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.payout_verification import PayoutMismatchKind, PayoutRunKind
from app.schemas.common import ORMModel


class PayoutUploadRead(ORMModel):
    id: uuid.UUID
    period_month: str
    kind: PayoutRunKind
    filename: str
    file_type: str
    identifier_columns: list[str]
    currency: str
    uploaded_by: uuid.UUID | None
    created_at: datetime
    row_count: int
    matched_count: int
    failed_count: int
    expected_count: int
    expected_total_minor: int
    paid_total_minor: int
    mismatch_count: int
    notified_at: datetime | None

    @field_validator("identifier_columns", mode="before")
    @classmethod
    def _split_columns(cls, value: object) -> object:
        if isinstance(value, str):
            return [part for part in value.split(",") if part]
        return value


class PayoutMismatchCount(BaseModel):
    kind: PayoutMismatchKind
    total: int
    open: int


class PayoutMismatchRead(BaseModel):
    id: uuid.UUID
    kind: PayoutMismatchKind
    employee_id: uuid.UUID | None
    employee_name: str | None
    employee_number: str | None
    # From the payout line itself, so an unmatched row is still recognisable.
    payee_name: str | None
    row_number: int | None
    reference: str | None
    account_last4: str | None
    # ACCOUNT_MISMATCH only: the last four of the account Avora has on file now.
    account_on_file_last4: str | None = None
    expected_minor: int | None
    paid_minor: int | None
    difference_minor: int  # paid - expected (missing side counts as 0)
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None
    resolved_by_name: str | None
    resolution_note: str | None


class PayoutVerificationRead(BaseModel):
    upload: PayoutUploadRead
    counts: list[PayoutMismatchCount]
    mismatches: list[PayoutMismatchRead]
    # Parse notes for the upload just made (e.g. account numbers Excel rounded).
    # Not stored: a later read of the same upload returns none.
    warnings: list[str] = Field(default_factory=list)


class PayoutMismatchResolve(BaseModel):
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A resolution note is required.")
        return stripped
