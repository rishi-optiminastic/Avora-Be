"""Events - something happening that HR or an admin wants people at.

A town hall, a training session, an offsite, a review window. The event says
what and when; a **form** attached to it is how answers are collected (an RSVP,
a feedback round, a sign-up), which is why there is no separate "event
response" table: `FormResponse` already stores answers, and a second mechanism
for the same thing would be a second place to look.

Audience is stored as a claim, not a list of people - `AudienceKind` plus the
departments or ids it names. "Everyone" written last month has to mean the
roster today, so the people are resolved at read time (`AudienceResolver`).
A snapshot would quietly exclude every joiner since.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.schemas.audience import AudienceKind


class EventStatus(StrEnum):
    DRAFT = "draft"  # being written; only its author and HR/admin see it
    PUBLISHED = "published"  # live; the audience sees it
    CANCELLED = "cancelled"  # called off, kept so people see it was


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_status_starts_at", "status", "starts_at"),)

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    # Free text: a room, a city, or a video link. Not modelled further because
    # half of these are "Zoom" and modelling that earns nothing.
    location: Mapped[str | None] = mapped_column(String(300), default=None)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Null for an open-ended event. An all-day event still stores real
    # timestamps (org midnight to midnight) so every query stays uniform.
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[EventStatus] = mapped_column(default=EventStatus.DRAFT, index=True)

    audience_kind: Mapped[AudienceKind] = mapped_column(default=AudienceKind.EVERYONE)
    audience_departments: Mapped[list[str]] = mapped_column(JSON, default=list)
    audience_employee_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    # The form that collects answers for this event, if there is one. SET NULL
    # rather than CASCADE: deleting a draft form must not delete the event that
    # referenced it, and an event with its form gone is still a real event.
    form_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("forms.id", ondelete="SET NULL"), default=None, index=True
    )
    # Set once the form has been sent to the audience, so publishing twice (or
    # re-publishing after an edit) cannot send it twice.
    form_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
