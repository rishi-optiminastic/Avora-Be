"""Note request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class NoteRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    body: str
    created_at: datetime
    updated_at: datetime
