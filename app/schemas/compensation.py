"""Compensation request/response schemas (Golden rule #5).

Amounts are integer minor units (cents). The client formats for display; we
never do float money on the server.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.compensation import PayPeriod
from app.schemas.common import ORMModel


class CompensationWrite(BaseModel):
    """HR/Admin set or replace an employee's current compensation."""

    amount_minor: int = Field(ge=0, le=10**15)
    bonus_minor: int = Field(default=0, ge=0, le=10**15)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    period: PayPeriod = PayPeriod.ANNUAL
    effective_date: date | None = None
    note: str | None = Field(default=None, max_length=500)


class CompensationRead(ORMModel):
    employee_id: uuid.UUID
    amount_minor: int
    bonus_minor: int
    currency: str
    period: PayPeriod
    effective_date: date | None
    note: str | None
    updated_by: uuid.UUID | None
    updated_at: datetime
