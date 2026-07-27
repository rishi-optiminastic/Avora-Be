"""Payroll adjustment request/response schemas (Golden rule #5)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.payroll_adjustment import PayrollAdjustmentKind, PayrollAdjustmentTarget
from app.schemas.common import ORMModel

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PayrollAdjustmentCreate(BaseModel):
    """HR/Admin add a manual adjustment to one employee's month.

    For EARNING/DEDUCTION, `amount_minor` is the money moved (must be positive) and
    `target` is omitted. For OVERRIDE, `target` is required and `amount_minor` is the
    forced value (money in minor units, or days x 100 when target is LOP_DAYS).
    """

    employee_id: uuid.UUID
    period_month: str
    kind: PayrollAdjustmentKind
    label: str = Field(min_length=1, max_length=200)
    amount_minor: int = Field(ge=0, le=100_000_000)
    target: PayrollAdjustmentTarget | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("period_month")
    @classmethod
    def _valid_month(cls, value: str) -> str:
        if not _MONTH_RE.match(value):
            raise ValueError("period_month must be YYYY-MM.")
        return value

    @model_validator(mode="after")
    def _kind_target_consistent(self) -> PayrollAdjustmentCreate:
        if self.kind is PayrollAdjustmentKind.OVERRIDE:
            if self.target is None:
                raise ValueError("An override must name a target field.")
        else:
            if self.target is not None:
                raise ValueError("Only overrides take a target field.")
            if self.amount_minor <= 0:
                raise ValueError("Earning/deduction amount must be positive.")
        return self


class PayrollAdjustmentRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    period_month: str
    kind: PayrollAdjustmentKind
    label: str
    amount_minor: int
    target: PayrollAdjustmentTarget | None
    note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
