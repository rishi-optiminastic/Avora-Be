"""Schemas for the personal browsing-privacy (hidden domains) feature."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class HiddenDomainCreate(BaseModel):
    """A domain (or full URL — the server re-derives the host) to hide."""

    domain: str = Field(min_length=1, max_length=256)


class HiddenDomainRead(ORMModel):
    id: uuid.UUID
    domain: str
    created_at: datetime
