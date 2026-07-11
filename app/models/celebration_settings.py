"""Celebration settings — the single org-wide toggle set for greeting emails.

One row (a singleton, upserted by the service), mirroring `eod_settings`. Holds
the on/off switches for the daily celebration run (birthday / work-anniversary /
festival greetings, all broadcast to the whole team) plus `last_run_on` — the
date the scheduler last processed, so a restart or extra tick never re-sends the
same day's greetings. HR/Admin edit it from Settings.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CelebrationSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "celebration_settings"

    birthday_enabled: Mapped[bool] = mapped_column(default=True)
    anniversary_enabled: Mapped[bool] = mapped_column(default=True)
    festival_enabled: Mapped[bool] = mapped_column(default=True)
    # The last date (org timezone) the daily run processed — idempotency marker.
    last_run_on: Mapped[date | None] = mapped_column(Date, default=None)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
