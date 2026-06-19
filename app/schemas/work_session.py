"""Work-session response schema (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import computed_field

from app.schemas.common import ORMModel


class WorkSessionRead(ORMModel):
    id: uuid.UUID
    clock_in_at: datetime
    clock_out_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.clock_out_at is None
