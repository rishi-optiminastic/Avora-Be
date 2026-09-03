"""Compensation request/response schemas (Golden rule #5).

Amounts are integer minor units (cents). The client formats for display; we
never do float money on the server. Bank details live here too (the account
number is encrypted at rest and only ever surfaced to the person or HR/Admin).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models.compensation import AccountType, Compensation, PayPeriod
from app.schemas.common import ORMModel

_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class CompensationWrite(BaseModel):
    """HR/Admin set or replace an employee's current compensation (pay only)."""

    amount_minor: int = Field(ge=0, le=10**15)
    bonus_minor: int = Field(default=0, ge=0, le=10**15)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    period: PayPeriod = PayPeriod.ANNUAL
    effective_date: date | None = None
    # Whether Provident Fund applies to this person. Defaults to True so an
    # older client that omits the field can never silently switch PF off.
    pf_enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


class BankDetailsWrite(BaseModel):
    """Salary-disbursal bank details. Editable by the person or HR/Admin.

    Separate from `CompensationWrite` because the pay amount is HR/Admin-only
    while an employee may maintain their own bank details.
    """

    account_holder_name: str | None = Field(default=None, max_length=128)
    bank_name: str | None = Field(default=None, max_length=128)
    account_number: str | None = Field(default=None, max_length=34)
    ifsc_code: str | None = Field(default=None, max_length=16)
    account_type: AccountType | None = None

    @field_validator("account_holder_name", "bank_name")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("account_number")
    @classmethod
    def _validate_account_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.isdigit() or not (6 <= len(cleaned) <= 20):
            raise ValueError("Account number must be 6-20 digits.")
        return cleaned

    @field_validator("ifsc_code")
    @classmethod
    def _validate_ifsc(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if not cleaned:
            return None
        if not _IFSC_RE.match(cleaned):
            raise ValueError("IFSC must be 11 chars, e.g. HDFC0001234.")
        return cleaned


class CompensationRead(ORMModel):
    employee_id: uuid.UUID
    amount_minor: int
    bonus_minor: int
    currency: str
    period: PayPeriod
    effective_date: date | None
    pf_enabled: bool
    note: str | None
    account_holder_name: str | None
    bank_name: str | None
    account_number: str | None  # decrypted for the authorized viewer; None if unset
    ifsc_code: str | None
    account_type: AccountType | None
    updated_by: uuid.UUID | None
    updated_at: datetime

    @classmethod
    def from_model(cls, model: Compensation, account_number: str | None) -> CompensationRead:
        """Build from the ORM row plus the already-decrypted account number."""
        return cls(
            employee_id=model.employee_id,
            amount_minor=model.amount_minor,
            bonus_minor=model.bonus_minor,
            currency=model.currency,
            period=model.period,
            effective_date=model.effective_date,
            pf_enabled=model.pf_enabled,
            note=model.note,
            account_holder_name=model.account_holder_name,
            bank_name=model.bank_name,
            account_number=account_number,
            ifsc_code=model.ifsc_code,
            account_type=model.account_type,
            updated_by=model.updated_by,
            updated_at=model.updated_at,
        )
