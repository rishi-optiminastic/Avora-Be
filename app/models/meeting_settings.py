"""Founder Meetings - the one row that says whose calendar this is.

A singleton, like `org_settings`: there is exactly one CEO queue per workspace,
and the alternative (a settings row per request, or constants in code) either
duplicates the answer or needs a deploy to change it.

`ceo_employee_id` is the only thing that makes the CEO approval queue resolvable,
and `ceo_calendar_id` is deliberately separate from it. The Founder's Office
books on the CEO's calendar, not their own, and assuming otherwise is the single
easiest way to put a meeting on the wrong person's day (spec section 8).
"""

from __future__ import annotations

import uuid
from datetime import time

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MeetingSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """How Founder Meetings behaves in this workspace. One row."""

    __tablename__ = "meeting_settings"

    # Null until an admin names the CEO. The service refuses to accept requests
    # at all in that state rather than queueing them for nobody, because a queue
    # with no reader is worse than a clear "not set up yet".
    ceo_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    # The calendar the event is actually written to. Empty means "the CEO's
    # primary calendar", resolved from their work email at call time.
    ceo_calendar_id: Mapped[str] = mapped_column(String(320), default="")

    # Whether the Founder's Office may confirm on its own. False is NOT "the
    # office is powerless" - it still rejects, asks for information and proposes
    # times - it only means every yes goes to the CEO before anything is booked.
    founders_office_may_confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    # Categories the CEO always decides personally, whatever the flag above says.
    # Values are `MeetingCategory`; JSON rather than an association table because
    # it is a short list read as a whole on every decision.
    categories_requiring_ceo: Mapped[list[str]] = mapped_column(JSON, default=list)

    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    # The CEO's bookable day, in the zone above. Availability outside it reads as
    # busy even when the calendar is empty, so the office does not offer 7am.
    working_hours_start: Mapped[time] = mapped_column(Time, default=time(10, 0))
    working_hours_end: Mapped[time] = mapped_column(Time, default=time(19, 0))
    # Minutes left clear either side of a booking, so back-to-back meetings do
    # not land the CEO with no gap at all.
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=10)

    # How far ahead a request may reach, and how little notice it may give.
    # Both are rejections at submit time with a plain reason, not silent clamps.
    booking_window_days: Mapped[int] = mapped_column(Integer, default=60)
    min_notice_hours: Mapped[int] = mapped_column(Integer, default=4)

    # Lead time on the "your meeting is soon" notification.
    reminder_lead_minutes: Mapped[int] = mapped_column(Integer, default=30)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
