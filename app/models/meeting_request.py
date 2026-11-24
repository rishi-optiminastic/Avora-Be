"""Founder Meetings - an employee's request to meet the CEO, and what it carries.

The problem this models: people ask the CEO for time informally (WhatsApp, a tap
on the shoulder, a forwarded mail), and the Founder's Office ends up as a human
queue with no record of who asked, what for, or what was decided. A request here
is that ask, written down once, with its agenda, its participants, its
attachments, and the slots anyone has proposed for it.

Four tables live in this module because they are only ever read together - a
request and the people, times and files hanging off it:

* `meeting_requests`    - the ask itself and where it has got to
* `meeting_participants` - who else should be in the room
* `meeting_slots`        - every time anyone has proposed, including alternatives
* `meeting_attachments`  - decks and documents the CEO should see first

Decisions are deliberately NOT here. They live in `meeting_approval.py` as an
append-only history, so "who approved this" survives a later reschedule or
cancellation (Security rule 5.7). The status column below is a cache of where
that history has arrived, never the record of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# The only durations the form offers. Enforced in the schema; kept here so the
# service and the migration's check constraint read from one list.
ALLOWED_DURATIONS = (15, 30, 45, 60)

# Guard rails, not business rules. A request that reaches these is a mistake
# rather than an unusual meeting.
MAX_PARTICIPANTS = 20
MAX_ALTERNATIVE_SLOTS = 5
MAX_ATTACHMENTS = 5


class MeetingCategory(StrEnum):
    PROJECT_UPDATE = "project_update"
    BUSINESS_STRATEGY = "business_strategy"
    TECHNICAL_DISCUSSION = "technical_discussion"
    BUDGET_FINANCE = "budget_finance"
    HR_PEOPLE = "hr_people"
    CLIENT_ESCALATION = "client_escalation"
    APPROVAL_REQUIRED = "approval_required"
    PERSONAL_CONFIDENTIAL = "personal_confidential"
    OTHER = "other"


class MeetingPriority(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class MeetingStatus(StrEnum):
    """Where a request has got to.

    Derived from the approval history on every transition, never set by a
    client. The terminal states are CONFIRMED -> COMPLETED, REJECTED and
    CANCELLED; SCHEDULING_FAILED is explicitly NOT terminal, because a calendar
    that was briefly unreachable must not lose a request both approvers already
    said yes to.
    """

    SUBMITTED = "submitted"  # waiting for the Founder's Office to look at it
    UNDER_REVIEW = "under_review"  # the Founder's Office has opened it
    NEEDS_INFORMATION = "needs_information"  # sent back to the requester
    AWAITING_CEO_APPROVAL = "awaiting_ceo_approval"  # forwarded to the CEO
    SLOT_PROPOSED = "slot_proposed"  # a different time is offered to the requester
    SLOT_ACCEPTED = "slot_accepted"  # the requester took a proposed time
    SCHEDULING = "scheduling"  # approved; the calendar write is in flight
    SCHEDULING_FAILED = "scheduling_failed"  # approved but Google refused; retryable
    CONFIRMED = "confirmed"  # on the calendar, invitations sent
    REJECTED = "rejected"  # declined by the Founder's Office or the CEO
    CANCELLED = "cancelled"  # called off after being raised
    COMPLETED = "completed"  # the meeting happened


# Statuses in which a requester may still pull their own request back. A
# confirmed meeting is NOT one of them: once it is on the CEO's calendar,
# withdrawal becomes a cancellation request the Founder's Office actions, so
# nobody vanishes from a calendar without the office knowing.
CANCELLABLE_BY_REQUESTER = frozenset(
    {
        MeetingStatus.SUBMITTED,
        MeetingStatus.UNDER_REVIEW,
        MeetingStatus.NEEDS_INFORMATION,
        MeetingStatus.AWAITING_CEO_APPROVAL,
        MeetingStatus.SLOT_PROPOSED,
    }
)

# Statuses that still need somebody to act. Drives the Founder's Office queue
# counts and the extension badge.
OPEN_STATUSES = frozenset(
    {
        MeetingStatus.SUBMITTED,
        MeetingStatus.UNDER_REVIEW,
        MeetingStatus.NEEDS_INFORMATION,
        MeetingStatus.AWAITING_CEO_APPROVAL,
        MeetingStatus.SLOT_PROPOSED,
        MeetingStatus.SLOT_ACCEPTED,
        MeetingStatus.SCHEDULING,
        MeetingStatus.SCHEDULING_FAILED,
    }
)


# Statuses the Founder's Office can still act on. SCHEDULING_FAILED is in the
# set deliberately: a calendar that was briefly unreachable is exactly the case
# the office needs to be able to retry, and leaving it out would strand a
# request both approvers had already approved.
FOUNDERS_OFFICE_ACTIONABLE = frozenset(
    {
        MeetingStatus.SUBMITTED,
        MeetingStatus.UNDER_REVIEW,
        MeetingStatus.NEEDS_INFORMATION,
        MeetingStatus.SLOT_PROPOSED,
        MeetingStatus.SLOT_ACCEPTED,
        MeetingStatus.SCHEDULING_FAILED,
    }
)

# Nothing further happens to these. CONFIRMED is NOT one of them: a confirmed
# meeting can still be cancelled, which is the whole point of having the office.
TERMINAL_STATUSES = frozenset(
    {MeetingStatus.REJECTED, MeetingStatus.CANCELLED, MeetingStatus.COMPLETED}
)


class MeetingRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_requests"
    __table_args__ = (
        # The Founder's Office queue: open requests, most urgent and earliest
        # wanted first.
        Index("ix_meeting_requests_status_preferred", "status", "preferred_start_at"),
        # "my requests", the employee popup's only list.
        Index("ix_meeting_requests_requester_status", "requester_id", "status"),
        # Upcoming / calendar reads, and the worker that completes past meetings.
        Index("ix_meeting_requests_scheduled", "scheduled_start_at"),
    )

    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[MeetingCategory] = mapped_column(default=MeetingCategory.OTHER, index=True)
    purpose: Mapped[str] = mapped_column(String(2000))
    agenda: Mapped[str] = mapped_column(String(8000))
    # What the requester wants to walk out with. Optional: a status update often
    # genuinely has no decision attached, and forcing a sentence there only
    # teaches people to type "NA".
    expected_outcome: Mapped[str | None] = mapped_column(String(2000), default=None)
    priority: Mapped[MeetingPriority] = mapped_column(default=MeetingPriority.NORMAL, index=True)

    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    # What the requester asked for. Kept untouched for the life of the request
    # even after a different slot is agreed, so "we moved you" stays visible
    # (spec: never silently change a requested time).
    preferred_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # IANA zone the request was composed in. Stored because a request raised from
    # a laptop in another country must render back in the zone its author meant,
    # and because the CEO's working-hours window is evaluated in the org's zone.
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    status: Mapped[MeetingStatus] = mapped_column(default=MeetingStatus.SUBMITTED, index=True)

    # The time actually agreed, set only when the calendar write succeeds. Null
    # on everything that is not yet CONFIRMED, which is what makes "upcoming"
    # answerable with one indexed range scan.
    scheduled_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # What the Founder's Office asked for when sending it back, so the requester
    # sees the question rather than just a status change.
    info_request_note: Mapped[str | None] = mapped_column(String(1000), default=None)

    cancel_reason: Mapped[str | None] = mapped_column(String(1000), default=None)
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Eagerly loaded: every list view shows participant count and attachment
    # count, so leaving these lazy would N+1 the queue on each page.
    participants: Mapped[list[MeetingParticipant]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MeetingParticipant.created_at",
    )
    slots: Mapped[list[MeetingSlot]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MeetingSlot.starts_at",
    )
    attachments: Mapped[list[MeetingAttachment]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MeetingAttachment.created_at",
    )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class MeetingParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Someone besides the requester and the CEO who should be in the room.

    Always an employee id, never a typed address: the form picks from the Avora
    directory, and `AudienceResolver` validates every id against the live roster
    before a row lands here. That is what stops a request from quietly inviting
    somebody who has left, or an address outside the company.
    """

    __tablename__ = "meeting_participants"
    __table_args__ = (
        UniqueConstraint(
            "meeting_request_id", "employee_id", name="uq_meeting_participants_request_employee"
        ),
    )

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    request: Mapped[MeetingRequest] = relationship(back_populates="participants")


class SlotOrigin(StrEnum):
    """Who put this time forward.

    The distinction matters for the rule that a meeting never moves without the
    employee agreeing: a slot the requester offered can be booked outright,
    while one an approver proposed has to be accepted first.
    """

    REQUESTER_PREFERRED = "requester_preferred"
    REQUESTER_ALTERNATIVE = "requester_alternative"
    FOUNDERS_OFFICE = "founders_office"
    CEO = "ceo"


class SlotStatus(StrEnum):
    OPEN = "open"  # on the table
    ACCEPTED = "accepted"  # the requester took it
    DECLINED = "declined"  # the requester turned it down
    SUPERSEDED = "superseded"  # a later proposal replaced it


class MeetingSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One proposed time for a request.

    Both the employee's alternatives and an approver's counter-proposals are
    rows here rather than two different shapes, because every consumer asks the
    same question of them - what times are on the table, and who offered each.
    """

    __tablename__ = "meeting_slots"
    __table_args__ = (Index("ix_meeting_slots_request_status", "meeting_request_id", "status"),)

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Carried per slot, not read off the request: an approver offering an
    # alternative often offers a shorter one ("I can give you 15 at 4").
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    origin: Mapped[SlotOrigin] = mapped_column(default=SlotOrigin.REQUESTER_ALTERNATIVE)
    proposed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[SlotStatus] = mapped_column(default=SlotStatus.OPEN, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    request: Mapped[MeetingRequest] = relationship(back_populates="slots")


class MeetingAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A document the CEO should see before the meeting.

    Bytes go to S3 when object storage is on - only `object_key` is stored -
    else they live in `content`, the same fallback reimbursement receipts,
    screenshots and workspace files use. Never served inline: the download route
    is scope-checked and hands the file back as a neutral-type attachment.
    """

    __tablename__ = "meeting_attachments"

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str | None] = mapped_column(String(255), default=None)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    request: Mapped[MeetingRequest] = relationship(back_populates="attachments")
