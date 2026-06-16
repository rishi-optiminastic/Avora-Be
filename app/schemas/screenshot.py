"""Screenshot response schemas (Golden rule #5). Image bytes are never returned
in a list — only metadata; the raw image is fetched one-by-one via its own
scoped endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import ORMModel


class ScreenshotRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    device_id: uuid.UUID
    captured_at: datetime
    received_at: datetime
    content_type: str
    width: int
    height: int
    byte_size: int
