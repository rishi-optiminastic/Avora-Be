"""Category-rule request/response schemas (Golden rule #5)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from app.core.categories import MatchType, ProductivityCategory

# Only these are assignable by a rule (idle/unknown are activity-state outcomes).
_ASSIGNABLE = {
    ProductivityCategory.PRODUCTIVE,
    ProductivityCategory.NEUTRAL,
    ProductivityCategory.UNPRODUCTIVE,
    ProductivityCategory.REQUIRES_REVIEW,
}


class CategoryRuleCreate(BaseModel):
    match_type: MatchType = MatchType.DOMAIN
    pattern: str = Field(min_length=1, max_length=256)
    category: ProductivityCategory
    department: str | None = Field(default=None, max_length=128)

    def assignable(self) -> bool:
        return self.category in _ASSIGNABLE


class CategoryRuleUpdate(BaseModel):
    match_type: MatchType | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=256)
    category: ProductivityCategory | None = None
    department: str | None = Field(default=None, max_length=128)


class CategoryRuleRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    match_type: MatchType
    pattern: str
    category: ProductivityCategory
    department: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
