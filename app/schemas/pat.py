"""Personal Access Token request/response schemas.

The ORM model is never returned directly (Golden rule #5). The raw token is
present only on `PatCreated`, returned once at creation and never stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PatCreate(BaseModel):
    label: str = Field(min_length=1, max_length=256)
    # Optional hard expiry. Omit for a non-expiring (but revocable) token.
    expires_at: datetime | None = None


class PatRead(BaseModel):
    id: uuid.UUID
    label: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_revoked: bool


class PatCreated(PatRead):
    # The raw token - returned ONCE at creation, never stored or shown again.
    token: str
