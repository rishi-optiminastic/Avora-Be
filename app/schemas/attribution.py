"""Work-attribution response schema (Golden rule #5)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class AttributionRead(BaseModel):
    """What one employee appears to be working on right now."""

    employee_id: uuid.UUID
    entity_id: uuid.UUID | None  # None ⇒ Unknown
    entity_name: str | None
    confidence: int  # 0 when Unknown
    matched: list[str]  # terms/domains that drove the match (for transparency)
