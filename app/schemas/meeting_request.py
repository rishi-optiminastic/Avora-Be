"""Founder Meetings request/response schemas (Golden rule #5).

Nothing a client sends decides anything. The requester comes from the
authenticated principal, the status comes from the approval history, and the
scheduled time comes from whatever Google confirmed. What a client may send is
only ever a *claim* about what it wants - which is why there is no `status`,
`requester_id` or `scheduled_start_at` on any Create or Update model below.
"""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.meeting_approval import ApprovalDecision, ApprovalStage
from app.models.meeting_event import EventSyncStatus
from app.models.meeting_request import (
    ALLOWED_DURATIONS,
    MAX_ALTERNATIVE_SLOTS,
    MAX_PARTICIPANTS,
    MeetingCategory,
    MeetingPriority,
    MeetingStatus,
    SlotOrigin,
    SlotStatus,
)
from app.schemas.common import ORMModel

_DURATION_HELP = "Duration must be one of 15, 30, 45 or 60 minutes."


def _check_duration(value: int) -> int:
    if value not in ALLOWED_DURATIONS:
        raise ValueError(_DURATION_HELP)
    return value


def _check_future_aware(value: datetime) -> datetime:
    """A proposed time must carry its offset and must not be in the past.

    Rejecting a naive datetime rather than assuming UTC is deliberate: a request
    composed at "15:00" with no offset is ambiguous by exactly the amount that
    puts a meeting on the wrong half of the day, and guessing is how a booking
    silently moves (see the task-recurrence naive-datetime bug).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Times must include a timezone offset.")
    if value <= datetime.now(UTC):
        raise ValueError("That time has already passed.")
    return value


class SlotProposal(BaseModel):
    """One time somebody puts forward - an alternative, or a counter-offer."""

    starts_at: datetime
    duration_minutes: int = 30

    _future = field_validator("starts_at")(_check_future_aware)
    _duration = field_validator("duration_minutes")(_check_duration)


class MeetingRequestCreate(BaseModel):
    """An employee asking for time with the CEO."""

    title: str = Field(min_length=3, max_length=200)
    category: MeetingCategory = MeetingCategory.OTHER
    purpose: str = Field(min_length=10, max_length=2000)
    agenda: str = Field(min_length=10, max_length=8000)
    expected_outcome: str | None = Field(default=None, max_length=2000)
    priority: MeetingPriority = MeetingPriority.NORMAL

    duration_minutes: int = 30
    preferred_start_at: datetime
    timezone: str = "Asia/Kolkata"

    # Other times that would also work. Not a courtesy: they are what lets the
    # Founder's Office book without a round trip when the first choice is busy.
    alternative_slots: list[SlotProposal] = Field(
        default_factory=list, max_length=MAX_ALTERNATIVE_SLOTS
    )
    # Employee ids from the Avora directory. Validated against the live roster
    # server-side (`AudienceResolver`), never trusted as a list of real people.
    participant_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_PARTICIPANTS)

    _preferred = field_validator("preferred_start_at")(_check_future_aware)
    _duration = field_validator("duration_minutes")(_check_duration)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("That is not a recognised timezone.") from exc
        return value

    @field_validator("participant_ids")
    @classmethod
    def _dedupe_participants(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _distinct_times(self) -> MeetingRequestCreate:
        """An alternative that repeats the preferred time, or another
        alternative, is a double-booked offer rather than a choice."""
        times = [self.preferred_start_at, *(s.starts_at for s in self.alternative_slots)]
        if len({t.astimezone(UTC) for t in times}) != len(times):
            raise ValueError("Each alternative time must be different.")
        return self


class MeetingRequestUpdate(BaseModel):
    """The requester answering a "needs more information" send-back.

    Only the fields the Founder's Office would have asked about. The time is
    editable here too, because "can you make it next week instead" is the most
    common thing they ask - but the status, approvals and anything already
    scheduled are not touchable from a client at all.
    """

    title: str | None = Field(default=None, min_length=3, max_length=200)
    category: MeetingCategory | None = None
    purpose: str | None = Field(default=None, min_length=10, max_length=2000)
    agenda: str | None = Field(default=None, min_length=10, max_length=8000)
    expected_outcome: str | None = Field(default=None, max_length=2000)
    priority: MeetingPriority | None = None
    duration_minutes: int | None = None
    preferred_start_at: datetime | None = None
    participant_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_PARTICIPANTS)

    @field_validator("duration_minutes")
    @classmethod
    def _duration(cls, value: int | None) -> int | None:
        return None if value is None else _check_duration(value)

    @field_validator("preferred_start_at")
    @classmethod
    def _preferred(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _check_future_aware(value)


class FoundersOfficeAction(StrEnum):
    """What the Founder's Office can do with a request.

    CONFIRM and FORWARD are both a yes. CONFIRM books it there and then (what
    the office does with a routine project update); FORWARD hands the decision
    to the CEO. Which of the two is available depends on `meeting_settings` and
    on the category.
    """

    CONFIRM = "confirm"
    FORWARD = "forward"
    REJECT = "reject"
    REQUEST_INFO = "request_info"
    PROPOSE_SLOT = "propose_slot"


class CeoAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PROPOSE_SLOT = "propose_slot"


class FoundersOfficeDecision(BaseModel):
    """The Founder's Office acting on one request."""

    action: FoundersOfficeAction
    note: str | None = Field(default=None, max_length=1000)
    # Required for PROPOSE_SLOT, ignored otherwise.
    slot: SlotProposal | None = None
    # For CONFIRM: which already-proposed time to book. Omitted means the
    # requester's preferred time.
    slot_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _needs_its_own_fields(self) -> FoundersOfficeDecision:
        if self.action is FoundersOfficeAction.PROPOSE_SLOT and self.slot is None:
            raise ValueError("Proposing a time needs that time.")
        if self.action is FoundersOfficeAction.REQUEST_INFO and not (self.note or "").strip():
            raise ValueError("Say what information is missing.")
        if self.action is FoundersOfficeAction.REJECT and not (self.note or "").strip():
            raise ValueError("Give a reason for the rejection.")
        return self


class CeoDecision(BaseModel):
    """The CEO acting on a forwarded request."""

    action: CeoAction
    note: str | None = Field(default=None, max_length=1000)
    slot: SlotProposal | None = None
    slot_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _needs_its_own_fields(self) -> CeoDecision:
        if self.action is CeoAction.PROPOSE_SLOT and self.slot is None:
            raise ValueError("Proposing a time needs that time.")
        if self.action is CeoAction.REJECT and not (self.note or "").strip():
            raise ValueError("Give a reason for the rejection.")
        return self


class DecisionNote(BaseModel):
    """An optional word of explanation on a decision."""

    note: str | None = Field(default=None, max_length=1000)


class RequiredNote(BaseModel):
    """A decision that must say why - a rejection, or a send-back.

    Required rather than optional because the requester reads it: "declined"
    with no reason is the thing this whole feature was built to replace.
    """

    note: str = Field(min_length=3, max_length=1000)


class ApprovePayload(DecisionNote):
    """Approving, and which of the times on the table to book.

    Omitting `slot_id` books the requester's preferred time, which is the common
    case and saves the client a lookup.
    """

    slot_id: uuid.UUID | None = None


class SuggestSlotPayload(DecisionNote):
    """Offering a different time. Still needs the requester's acceptance."""

    slot: SlotProposal


class SlotResponse(BaseModel):
    """The requester answering a proposed time. This is the consent that stops a
    meeting being moved underneath somebody."""

    accept: bool
    note: str | None = Field(default=None, max_length=1000)


class MeetingCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class MeetingReschedule(BaseModel):
    """The Founder's Office moving a confirmed meeting.

    Still a proposal, not a move: the requester is asked to accept it, because
    the one rule this feature cannot break is changing somebody's meeting
    without telling them.
    """

    starts_at: datetime
    duration_minutes: int = 30
    reason: str = Field(min_length=3, max_length=1000)

    _future = field_validator("starts_at")(_check_future_aware)
    _duration = field_validator("duration_minutes")(_check_duration)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
class MeetingSlotRead(ORMModel):
    id: uuid.UUID
    starts_at: datetime
    duration_minutes: int
    origin: SlotOrigin
    proposed_by_id: uuid.UUID | None
    status: SlotStatus
    responded_at: datetime | None


class MeetingParticipantRead(ORMModel):
    employee_id: uuid.UUID
    # Resolved at read time, never stored: a name frozen at write time goes
    # stale the first time somebody marries or corrects their spelling.
    full_name: str | None = None


class MeetingAttachmentRead(ORMModel):
    """One document on a request. No bytes and no storage key - the file comes
    through the scoped download route or not at all."""

    id: uuid.UUID
    label: str
    filename: str | None
    content_type: str
    size_bytes: int
    created_at: datetime


class MeetingApprovalRead(ORMModel):
    id: uuid.UUID
    stage: ApprovalStage
    decision: ApprovalDecision
    decided_by_id: uuid.UUID | None
    decided_by_name: str | None = None
    note: str | None
    proposed_slot_id: uuid.UUID | None
    decided_at: datetime


class MeetingActivityRead(ORMModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    detail: str | None
    created_at: datetime


class MeetingEventRead(ORMModel):
    """Where the meeting lives at Google. `last_error` is included because a
    scheduling failure the Founder's Office cannot see is one they cannot fix."""

    google_event_id: str | None
    html_link: str | None
    meet_url: str | None
    sync_status: EventSyncStatus
    attempt_count: int
    last_error: str | None


class MeetingRequestRead(ORMModel):
    """A request as it appears in a list."""

    id: uuid.UUID
    requester_id: uuid.UUID
    requester_name: str | None = None
    title: str
    category: MeetingCategory
    priority: MeetingPriority
    status: MeetingStatus
    duration_minutes: int
    preferred_start_at: datetime
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    timezone: str
    participant_count: int = 0
    attachment_count: int = 0
    created_at: datetime
    updated_at: datetime

    # What the CALLER may do with it, from the same rules the action endpoints
    # enforce. The queues and buttons read these rather than re-deriving
    # authorization in the client, where it would be both duplicated and
    # unenforceable.
    can_decide: bool = False
    can_cancel: bool = False
    can_respond_to_slot: bool = False


class MeetingRequestDetail(MeetingRequestRead):
    """One request, opened: everything a reviewer needs to decide."""

    purpose: str
    agenda: str
    expected_outcome: str | None
    info_request_note: str | None
    cancel_reason: str | None
    participants: list[MeetingParticipantRead] = Field(default_factory=list)
    slots: list[MeetingSlotRead] = Field(default_factory=list)
    attachments: list[MeetingAttachmentRead] = Field(default_factory=list)
    approvals: list[MeetingApprovalRead] = Field(default_factory=list)
    activity: list[MeetingActivityRead] = Field(default_factory=list)
    event: MeetingEventRead | None = None


class MeetingQueueCounts(BaseModel):
    """What the extension badge and the dashboard tiles show. One request
    instead of five list calls the popup would otherwise make on every open."""

    awaiting_founders_office: int = 0
    awaiting_ceo: int = 0
    awaiting_requester: int = 0
    scheduling_failed: int = 0
    confirmed_today: int = 0
    my_open_requests: int = 0


class MeetingSettingsRead(ORMModel):
    ceo_employee_id: uuid.UUID | None
    ceo_name: str | None = None
    ceo_calendar_id: str
    founders_office_may_confirm: bool
    categories_requiring_ceo: list[str]
    timezone: str
    buffer_minutes: int
    booking_window_days: int
    min_notice_hours: int
    reminder_lead_minutes: int
    # Whether the feature can actually run: a CEO is named and Google is wired.
    configured: bool = False


class MeetingSettingsUpdate(BaseModel):
    """Admin-only. Every field optional; omitted means "leave it alone"."""

    ceo_employee_id: uuid.UUID | None = None
    ceo_calendar_id: str | None = Field(default=None, max_length=320)
    founders_office_may_confirm: bool | None = None
    categories_requiring_ceo: list[MeetingCategory] | None = None
    timezone: str | None = None
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    booking_window_days: int | None = Field(default=None, ge=1, le=365)
    min_notice_hours: int | None = Field(default=None, ge=0, le=720)
    reminder_lead_minutes: int | None = Field(default=None, ge=0, le=1440)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("That is not a recognised timezone.") from exc
        return value
