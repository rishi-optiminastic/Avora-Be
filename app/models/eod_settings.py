"""End-of-Day schedule — the single org-wide config for the EOD report run.

One row (a singleton, upserted by the service), mirroring `attendance_policy`.
Holds the on/off switch and the two local-clock times (in the attendance-policy
timezone) that drive the scheduler: when drafts are generated and when they
auto-send to managers. Secrets (the OpenRouter key + model) stay in env — only
the schedule and the toggle live here so Admin/HR can tune them from Settings
without a redeploy. Times are stored as hour + minute; the worker compares them
against the local clock each tick.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EodSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "eod_settings"

    # Master on/off for the automatic run (manual generate/approve are unaffected).
    enabled: Mapped[bool] = mapped_column(default=False)
    # DRAFT time — generate each present employee's draft and notify them to
    # review/edit (default 16:30 local = 4:30 PM IST).
    draft_hour: Mapped[int] = mapped_column(default=16)
    draft_minute: Mapped[int] = mapped_column(default=30)
    # SEND time — email the (edited-or-as-is) report to manager + admins
    # (default 18:00 local = 6:00 PM IST). The draft→send gap is the review window.
    send_hour: Mapped[int] = mapped_column(default=18)
    send_minute: Mapped[int] = mapped_column(default=0)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
