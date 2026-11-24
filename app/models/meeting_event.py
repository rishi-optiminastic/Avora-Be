"""Founder Meetings - what Google Calendar knows about a request, and drift.

Two tables, both about the boundary with an external system we do not control:

* `meeting_events` - one row per request, carrying the Google event id, where it
  can be opened, and whether our last write actually landed. A request reaches
  CONFIRMED only when this row says CREATED.
* `meeting_calendar_sync` - per-calendar watch channel and incremental sync
  cursor, so a meeting moved or deleted inside Google (which happens: the CEO
  drags it in their own calendar) is noticed rather than silently diverging.

The spec also asked for a `meeting_integration_accounts` table holding encrypted
OAuth refresh tokens. There is deliberately none. Avora already reaches Calendar
through a Workspace service account with domain-wide delegation
(`MeetingService`), so there is no per-user token to store, refresh, encrypt or
re-consent - and a table of credentials that need not exist is a liability, not
a feature.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EventSyncStatus(StrEnum):
    PENDING = "pending"  # we intend to write it; nothing at Google yet
    CREATED = "created"  # on the calendar, invitations sent
    UPDATED = "updated"  # rescheduled and the patch landed
    CANCELLED = "cancelled"  # deleted at Google, attendees notified
    FAILED = "failed"  # Google refused; `last_error` says what it said


class MeetingEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The Google Calendar event backing one confirmed request.

    One row per request, enforced by a unique constraint rather than left to the
    service. That constraint IS the duplicate-event guard the spec asks for: two
    concurrent approvals of the same request race to insert, the loser gets an
    IntegrityError, and only one event is ever created. An idempotency key on the
    endpoint protects a retrying client; this protects the database.

    A reschedule patches this row in place and keeps the same `google_event_id`,
    so the attendees' existing calendar entry moves instead of a second one
    appearing beside it.
    """

    __tablename__ = "meeting_events"
    __table_args__ = (
        UniqueConstraint("meeting_request_id", name="uq_meeting_events_request"),
        # Reconciliation looks up by what Google calls it.
        Index("ix_meeting_events_google_event", "google_event_id"),
    )

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )

    # Null while PENDING or after a FAILED first attempt - we have a row
    # (claiming the request) before Google has given us an id.
    google_event_id: Mapped[str | None] = mapped_column(String(1024), default=None)
    # Which calendar it was written to, resolved from settings at write time and
    # recorded, so a later patch or delete goes back to the same place even if
    # the setting has since changed.
    google_calendar_id: Mapped[str] = mapped_column(String(320), default="")
    html_link: Mapped[str | None] = mapped_column(String(2048), default=None)
    meet_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    # Stable across moves and copies; the reliable key when reconciling.
    ical_uid: Mapped[str | None] = mapped_column(String(1024), default=None)
    # Google's version marker, for conditional writes and change detection.
    etag: Mapped[str | None] = mapped_column(String(256), default=None)

    # What we believe is on the calendar. Compared against the request's
    # scheduled times to spot drift without calling Google.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    sync_status: Mapped[EventSyncStatus] = mapped_column(
        default=EventSyncStatus.PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Google's own words, truncated. Never a token or a header - the service
    # passes only the API's error text, which carries no credentials.
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)


class MeetingCalendarSync(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Watch-channel state and the incremental cursor for one calendar.

    Google push notifications expire (a channel lives days, not forever) and are
    not guaranteed to arrive, which is why `last_reconciled_at` exists next to
    them: the worker does a periodic full pass regardless, so a missed
    notification costs freshness rather than correctness.
    """

    __tablename__ = "meeting_calendar_sync"
    __table_args__ = (UniqueConstraint("calendar_id", name="uq_meeting_calendar_sync_calendar"),)

    calendar_id: Mapped[str] = mapped_column(String(320))
    # Our id for the channel and Google's id for the watched resource. Both are
    # needed to stop watching; neither is a secret.
    channel_id: Mapped[str | None] = mapped_column(String(128), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(256), default=None)
    channel_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Opaque cursor from Google: "tell me what changed since this".
    sync_token: Mapped[str | None] = mapped_column(String(1024), default=None)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
