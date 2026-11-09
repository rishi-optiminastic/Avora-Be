"""Access request/response schemas (Golden rule #5).

No read schema here carries a secret. The password or key leaves the server
only through `SecretRevealRead`, returned by the audited reveal endpoint after
the grant is re-checked. Fields a caller may not see (cost, login id, billing
details) arrive as null with `cost_visible` / `details_visible` saying why, so
the UI never has to guess.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.access import (
    AccessLevel,
    AccessRequestStatus,
    AssetPriority,
    AssetStatus,
    TechAssetKind,
)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_SECRET = 4000
_MAX_DETAIL_KEYS = 40
_MAX_DETAIL_VALUE = 500
_MINOR_MAX = 10**13
# `details` is for registrar names and plan tiers, never credentials. A key that
# looks like one is refused so a secret cannot slip past the encrypted column.
_SECRET_LIKE = re.compile(r"pass|secret|token|api[_ ]?key|otp|pin\b|private", re.IGNORECASE)

DetailValue = str | int | float | bool | None


def _clean_details(value: dict[str, Any]) -> dict[str, DetailValue]:
    if len(value) > _MAX_DETAIL_KEYS:
        raise ValueError(f"details holds at most {_MAX_DETAIL_KEYS} fields")
    out: dict[str, DetailValue] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not 0 < len(key) <= 64:
            raise ValueError("each details key is 1 to 64 characters")
        if _SECRET_LIKE.search(key):
            raise ValueError(f"'{key}' looks like a credential; store it as the asset's secret")
        if raw is not None and not isinstance(raw, str | int | float | bool):
            raise ValueError("details values are plain text, numbers or yes/no")
        if isinstance(raw, str) and len(raw) > _MAX_DETAIL_VALUE:
            raise ValueError(f"'{key}' is longer than {_MAX_DETAIL_VALUE} characters")
        out[key] = raw
    return out


def _upper_currency(value: str | None) -> str | None:
    if value is None:
        return None
    upper = value.strip().upper()
    if not _CURRENCY_RE.match(upper):
        raise ValueError("currency is a three-letter code such as INR or USD")
    return upper


class _AssetFields(BaseModel):
    """Fields shared by create and update; every one optional on update."""

    category: str | None = Field(default=None, max_length=64)
    parent_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    login_url: str | None = Field(default=None, max_length=500)
    login_id: str | None = Field(default=None, max_length=320)
    two_factor_method: str | None = Field(default=None, max_length=64)
    cost_minor: int | None = Field(default=None, ge=0, le=_MINOR_MAX)
    currency: str | None = None
    billing_cycle: str | None = Field(default=None, max_length=32)
    renews_on: date | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return _upper_currency(value)


class TechAssetCreate(_AssetFields):
    kind: TechAssetKind
    name: str = Field(min_length=1, max_length=200)
    priority: AssetPriority = AssetPriority.MEDIUM
    status: AssetStatus = AssetStatus.ACTIVE
    details: dict[str, Any] = Field(default_factory=dict)
    # Optional on create; encrypted before it touches the session.
    secret: str | None = Field(default=None, min_length=1, max_length=_MAX_SECRET)

    @field_validator("details")
    @classmethod
    def _details(cls, value: dict[str, Any]) -> dict[str, DetailValue]:
        return _clean_details(value)


class TechAssetUpdate(_AssetFields):
    """Partial update. Only fields the client sends are applied (exclude_unset),
    so sending `parent_id: null` detaches, and leaving it out keeps it."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    priority: AssetPriority | None = None
    status: AssetStatus | None = None
    details: dict[str, Any] | None = None
    archived: bool | None = None

    @field_validator("details")
    @classmethod
    def _details(cls, value: dict[str, Any] | None) -> dict[str, DetailValue] | None:
        return None if value is None else _clean_details(value)


# Tone keys the design system knows; a brand's colour must be one of them.
BRAND_COLORS = ("violet", "sage", "pink", "butter", "orange", "sky")


class _BrandFields(BaseModel):
    website: str | None = Field(default=None, max_length=255)
    color: str | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("color")
    @classmethod
    def _tone(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if value not in BRAND_COLORS:
            raise ValueError(f"color is one of {', '.join(BRAND_COLORS)}")
        return value


class BrandCreate(_BrandFields):
    name: str = Field(min_length=1, max_length=120)


class BrandUpdate(_BrandFields):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class BrandRead(BaseModel):
    id: uuid.UUID
    name: str
    website: str | None
    color: str | None
    notes: str | None
    asset_count: int


class SecretWrite(BaseModel):
    secret: str = Field(min_length=1, max_length=_MAX_SECRET)


class SecretRevealRead(BaseModel):
    """The only shape that carries plaintext. Returned once per reveal."""

    asset_id: uuid.UUID
    login_id: str | None
    secret: str


class HolderRead(BaseModel):
    grant_id: uuid.UUID
    employee_id: uuid.UUID
    name: str
    department: str | None
    level: AccessLevel
    granted_at: datetime


class TechAssetRead(BaseModel):
    id: uuid.UUID
    kind: TechAssetKind
    name: str
    category: str | None
    parent_id: uuid.UUID | None
    parent_name: str | None
    owner_id: uuid.UUID | None
    owner_name: str | None
    brand_id: uuid.UUID | None
    brand_name: str | None
    login_url: str | None
    priority: AssetPriority
    status: AssetStatus
    two_factor_method: str | None
    has_secret: bool
    secret_rotated_at: datetime | None
    renews_on: date | None
    archived: bool
    # Live holders in total, a plain count everyone may see; `holders` lists
    # only the ones inside the caller's scope.
    holder_count: int
    holders: list[HolderRead]
    my_level: AccessLevel | None
    # Gated: null unless the caller holds it, runs a team that does, or is HR/Admin.
    details_visible: bool
    login_id: str | None
    details: dict[str, DetailValue] | None
    notes: str | None
    cost_visible: bool
    cost_minor: int | None
    currency: str | None
    billing_cycle: str | None


class MyGrantRead(BaseModel):
    grant_id: uuid.UUID
    level: AccessLevel
    granted_at: datetime
    asset: TechAssetRead


class GrantCreate(BaseModel):
    asset_id: uuid.UUID
    employee_id: uuid.UUID
    level: AccessLevel = AccessLevel.VIEWER
    note: str | None = Field(default=None, max_length=200)


class GrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    asset_id: uuid.UUID
    employee_id: uuid.UUID
    level: AccessLevel
    granted_at: datetime
    revoked_at: datetime | None


class AccessRequestCreate(BaseModel):
    asset_id: uuid.UUID
    level: AccessLevel = AccessLevel.VIEWER
    reason: str = Field(min_length=1, max_length=500)


class AccessRequestReview(BaseModel):
    approve: bool
    review_note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reject_says_why(self) -> AccessRequestReview:
        if not self.approve and not (self.review_note and self.review_note.strip()):
            raise ValueError("say why when rejecting a request")
        return self


class AccessRequestRead(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    asset_name: str
    asset_kind: TechAssetKind
    employee_id: uuid.UUID
    employee_name: str
    level: AccessLevel
    reason: str
    status: AccessRequestStatus
    reviewed_by_id: uuid.UUID | None
    reviewer_name: str | None
    decided_at: datetime | None
    review_note: str | None
    created_at: datetime
    # The server's verdict on whether THIS caller may decide THIS request.
    can_decide: bool


# ---- spend (finance) -------------------------------------------------------- #


class SpendTotalRead(BaseModel):
    currency: str
    monthly_minor: int
    annual_minor: int


class SpendGroupRead(BaseModel):
    label: str
    count: int
    totals: list[SpendTotalRead]


class SpendLineRead(BaseModel):
    asset_id: uuid.UUID
    name: str
    brand: str
    kind: TechAssetKind
    category: str
    team: str
    owner_name: str | None
    billed_to: str | None
    status: str
    billing_cycle: str | None
    currency: str
    cost_minor: int
    monthly_minor: int
    annual_minor: int
    renews_on: date | None
    payment_method: str | None
    card_last4: str | None
    auto_renew: str | None
    recurring: bool
    estimated: bool
    counted: bool


class SpendSummaryRead(BaseModel):
    """What the company spends on tech, for finance. Rupees and dollars are
    totalled separately; nothing is converted on the server."""

    generated_on: date
    count: int
    totals: list[SpendTotalRead]
    by_brand: list[SpendGroupRead]
    by_team: list[SpendGroupRead]
    by_category: list[SpendGroupRead]
    by_kind: list[SpendGroupRead]
    renewals: list[SpendLineRead]
    lines: list[SpendLineRead]
