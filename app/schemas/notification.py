"""Notification API schemas — read-only to the client; creation is internal."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.notification import NotificationKind, NotificationLevel
from app.schemas.common import ORMModel


class NotificationRead(ORMModel):
    id: uuid.UUID
    kind: NotificationKind
    level: NotificationLevel
    title: str
    body: str | None
    link: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class UnreadCount(BaseModel):
    count: int
