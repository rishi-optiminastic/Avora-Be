"""Work-session response schema (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, computed_field

from app.schemas.common import ORMModel


class WorkSessionRead(ORMModel):
    id: uuid.UUID
    clock_in_at: datetime
    clock_out_at: datetime | None
    ip_address: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.clock_out_at is None


class BiometricTodayRead(BaseModel):
    """The caller's biometric check-in for today (navbar timer). Biometric only —
    the agent/manual sessions are deliberately not used for this."""

    clock_in_at: datetime
    clock_out_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.clock_out_at is None
