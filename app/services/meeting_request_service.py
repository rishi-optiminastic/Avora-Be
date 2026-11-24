"""Founder Meetings - the approval state machine.

Three people act on a request and the rules differ for each, so authorization is
answered once per actor here and never in a client:

* the **requester** submits, answers a send-back, accepts or refuses a proposed
  time, and withdraws while it is still unscheduled;
* the **Founder's Office** (`employees.founders_office`) triages everything -
  reject, ask for more, propose another time, confirm outright, or forward;
* the **CEO** (`meeting_settings.ceo_employee_id`) decides what is forwarded.

Two rules run through all of it. First, a meeting NEVER moves without the
requester agreeing: a time proposed by an approver becomes bookable only once
the requester accepts it, which is why `SLOT_PROPOSED` and `SLOT_ACCEPTED` are
distinct states rather than a flag. Second, a decision is durable even when the
booking that follows it fails - see `MeetingBookingService`.

Notification bodies deliberately never carry the purpose or the agenda. A
request can be Personal / Confidential, and an inbox preview is the one place
its subject would reach a phone's lock screen.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.time import as_utc
from app.core.uploads import detect_media_type
from app.models.meeting_approval import ApprovalDecision, ApprovalStage, MeetingApproval
from app.models.meeting_request import (
    CANCELLABLE_BY_REQUESTER,
    FOUNDERS_OFFICE_ACTIONABLE,
    MAX_ATTACHMENTS,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    MeetingAttachment,
    MeetingCategory,
    MeetingRequest,
    MeetingSlot,
    MeetingStatus,
    SlotOrigin,
    SlotStatus,
)
from app.models.meeting_settings import MeetingSettings
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.meeting_request import MeetingRequestRepository, MeetingViewer
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.schemas.audience import AudienceKind, AudienceSpec
from app.schemas.auth import CurrentUser
from app.schemas.meeting_request import (
    CeoAction,
    CeoDecision,
    FoundersOfficeAction,
    FoundersOfficeDecision,
    MeetingActivityRead,
    MeetingApprovalRead,
    MeetingAttachmentRead,
    MeetingCancel,
    MeetingEventRead,
    MeetingParticipantRead,
    MeetingQueueCounts,
    MeetingRequestCreate,
    MeetingRequestDetail,
    MeetingRequestRead,
    MeetingRequestUpdate,
    MeetingSettingsRead,
    MeetingSettingsUpdate,
    MeetingSlotRead,
    SlotProposal,
    SlotResponse,
    SuggestSlotPayload,
)
from app.services.audience import AudienceResolver
from app.services.meeting_booking_service import MeetingBookingService
from app.services.notification_service import NotificationService

logger = get_logger("app.meeting_request")

_ENTITY = "meeting_request"

# A deck or a one-pager, not a file share. Same ceiling and the same four
# formats as a reimbursement proof, because the sniffer only vouches for
# these and a type nobody can verify is a type nobody should accept.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})


class MeetingRequestService:
    def __init__(
        self,
        settings: Settings,
        requests: MeetingRequestRepository,
        employees: EmployeeRepository,
        meeting_settings: MeetingSettingsRepository,
        booking: MeetingBookingService,
        notifications: NotificationService,
        audit: AuditRepository,
    ) -> None:
        self._app_settings = settings
        self._requests = requests
        self._employees = employees
        self._settings = meeting_settings
        self._booking = booking
        self._notifications = notifications
        self._audit = audit
        self._audience = AudienceResolver(employees)

    # ---- who is asking ---------------------------------------------------- #
    async def _viewer(self, caller: CurrentUser) -> MeetingViewer:
        """The one place "what may this person see" is decided.

        Being the CEO is a settings lookup rather than a token claim, so it is
        resolved here and nowhere else.
        """
        config = await self._settings.get_or_create()
        return MeetingViewer(
            employee_id=caller.employee_id,
            is_founders_office=caller.founders_office,
            is_ceo=config.ceo_employee_id == caller.employee_id,
        )

    async def _require_founders_office(self, caller: CurrentUser) -> None:
        if not caller.founders_office:
            raise AuthorizationError("Only the Founder's Office can action requests.")

    async def _require_ceo(self, caller: CurrentUser) -> MeetingSettings:
        config = await self._settings.get_or_create()
        if config.ceo_employee_id != caller.employee_id:
            raise AuthorizationError("Only the CEO can decide a forwarded request.")
        return config

    async def _load(self, caller: CurrentUser, request_id: uuid.UUID) -> MeetingRequest:
        """A request this caller may see, or a 404.

        404 rather than 403 on purpose: a stranger guessing ids must not learn
        that a meeting exists, let alone who asked for one (API conventions).
        """
        viewer = await self._viewer(caller)
        row = await self._requests.get_in_scope(viewer, request_id)
        if row is None:
            raise NotFoundError("That meeting request could not be found.")
        return row

    # ---- submit ----------------------------------------------------------- #
    async def submit(self, caller: CurrentUser, payload: MeetingRequestCreate) -> MeetingRequest:
        config = await self._settings.get_or_create()
        if config.ceo_employee_id is None:
            raise ValidationError(
                "Founder Meetings is not set up yet - an admin needs to name the CEO."
            )
        if config.ceo_employee_id == caller.employee_id:
            raise ValidationError("You cannot request a meeting with yourself.")

        self._check_timing(payload.preferred_start_at, config)
        for slot in payload.alternative_slots:
            self._check_timing(slot.starts_at, config)

        participant_ids = await self._resolve_participants(
            payload.participant_ids, caller.employee_id, config.ceo_employee_id
        )

        row = await self._requests.create(
            requester_id=caller.employee_id,
            fields={
                "title": payload.title.strip(),
                "category": payload.category,
                "purpose": payload.purpose.strip(),
                "agenda": payload.agenda.strip(),
                "expected_outcome": (payload.expected_outcome or "").strip() or None,
                "priority": payload.priority,
                "duration_minutes": payload.duration_minutes,
                "preferred_start_at": payload.preferred_start_at,
                "timezone": payload.timezone,
            },
        )
        if participant_ids:
            await self._requests.replace_participants(row, participant_ids)

        # The preferred time is a slot like any other, so every consumer asks one
        # question ("what times are on the table") instead of special-casing it.
        await self._requests.add_slot(
            row,
            starts_at=payload.preferred_start_at,
            duration_minutes=payload.duration_minutes,
            origin=SlotOrigin.REQUESTER_PREFERRED,
            proposed_by_id=caller.employee_id,
        )
        for slot in payload.alternative_slots:
            await self._requests.add_slot(
                row,
                starts_at=slot.starts_at,
                duration_minutes=slot.duration_minutes,
                origin=SlotOrigin.REQUESTER_ALTERNATIVE,
                proposed_by_id=caller.employee_id,
            )

        await self._requests.log(request_id=row.id, actor_id=caller.employee_id, action="submitted")
        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.request_submitted",
            target=f"{_ENTITY}:{row.id}",
        )
        await self._alert_founders_office(row, caller.employee_id)
        return row

    def _check_timing(self, when: datetime, config: MeetingSettings) -> None:
        """Notice and booking-window limits, as rejections with a reason.

        Clamping silently to the nearest legal time would be the same class of
        bug as moving a confirmed meeting: the person asked for something
        specific and deserves to be told it is not possible.
        """
        now = datetime.now(UTC)
        when = as_utc(when)
        if when < now + timedelta(hours=config.min_notice_hours):
            raise ValidationError(
                f"Requests need at least {config.min_notice_hours} hours' notice."
            )
        if when > now + timedelta(days=config.booking_window_days):
            raise ValidationError(
                f"Requests cannot be more than {config.booking_window_days} days ahead."
            )

    async def _resolve_participants(
        self,
        requested: Sequence[uuid.UUID],
        requester_id: uuid.UUID,
        ceo_id: uuid.UUID | None,
    ) -> list[uuid.UUID]:
        """Validate the room against the live roster.

        Reuses `AudienceResolver`, so an id that is not a current employee is a
        422 rather than a silent omission, and a departed colleague cannot be
        invited by an id copied from an old request. The requester and the CEO
        are dropped rather than rejected: they are already in the meeting, and
        failing the whole submission over it would be pedantry.
        """
        wanted = [i for i in dict.fromkeys(requested) if i not in (requester_id, ceo_id)]
        if not wanted:
            return []
        people = await self._audience.resolve(
            AudienceSpec(kind=AudienceKind.PEOPLE, employee_ids=list(wanted))
        )
        return [p.id for p in people]

    # ---- reads ------------------------------------------------------------ #
    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: MeetingStatus | None = None,
        mine_only: bool = False,
        scheduled_from: datetime | None = None,
        scheduled_to: datetime | None = None,
    ) -> tuple[Sequence[MeetingRequest], int]:
        viewer = await self._viewer(caller)
        return await self._requests.list_for_scope(
            viewer,
            offset=offset,
            limit=limit,
            status=status,
            mine_only=mine_only,
            scheduled_from=scheduled_from,
            scheduled_to=scheduled_to,
        )

    async def get_for_caller(self, caller: CurrentUser, request_id: uuid.UUID) -> MeetingRequest:
        return await self._load(caller, request_id)

    async def mark_under_review(self, caller: CurrentUser, request_id: uuid.UUID) -> MeetingRequest:
        """The Founder's Office picking a request up.

        An explicit action rather than a side effect of opening it: a GET that
        writes is neither cacheable nor safe to retry, and "somebody is on this"
        is worth saying deliberately when several people share the queue.
        """
        await self._require_founders_office(caller)
        row = await self._load(caller, request_id)
        if row.status is not MeetingStatus.SUBMITTED:
            return row
        row.status = MeetingStatus.UNDER_REVIEW
        await self._requests.log(
            request_id=row.id, actor_id=caller.employee_id, action="under_review"
        )
        await self._requests.flush()
        return row

    async def queue_counts(self, caller: CurrentUser) -> MeetingQueueCounts:
        viewer = await self._viewer(caller)
        by_status = await self._requests.count_by_status(viewer)
        day_start, day_end = await self._today_bounds()
        _, confirmed_today = await self._requests.list_for_scope(
            viewer,
            offset=0,
            limit=1,
            status=MeetingStatus.CONFIRMED,
            scheduled_from=day_start,
            scheduled_to=day_end,
        )
        return MeetingQueueCounts(
            awaiting_founders_office=by_status.get(MeetingStatus.SUBMITTED, 0)
            + by_status.get(MeetingStatus.UNDER_REVIEW, 0)
            + by_status.get(MeetingStatus.SLOT_ACCEPTED, 0),
            awaiting_ceo=by_status.get(MeetingStatus.AWAITING_CEO_APPROVAL, 0),
            awaiting_requester=by_status.get(MeetingStatus.NEEDS_INFORMATION, 0)
            + by_status.get(MeetingStatus.SLOT_PROPOSED, 0),
            scheduling_failed=by_status.get(MeetingStatus.SCHEDULING_FAILED, 0),
            confirmed_today=confirmed_today,
            my_open_requests=await self._requests.count_own_open(
                caller.employee_id, [s.value for s in OPEN_STATUSES]
            ),
        )

    async def _today_bounds(self) -> tuple[datetime, datetime]:
        """Midnight to midnight in the workspace's own timezone, as UTC.

        A UTC day boundary is 05:30 in Asia/Kolkata, so counting "confirmed
        today" from UTC midnight reported yesterday's meetings for the first
        five and a half hours of every working morning - exactly when somebody
        opens the dashboard to see what the day holds.
        """
        config = await self._settings.get_or_create()
        zone: tzinfo
        try:
            zone = ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A zone that no longer resolves must not take the whole popup down
            # with it; UTC is wrong by hours, an exception is wrong entirely.
            zone = UTC
        local = datetime.now(zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    # ---- requester edits -------------------------------------------------- #
    async def update(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: MeetingRequestUpdate
    ) -> MeetingRequest:
        """The requester answering a send-back, or correcting their own request.

        Only before anybody has committed to a time. Once a request is with the
        CEO or on a calendar, an edit would change what was approved underneath
        the approver, so it is refused and the requester cancels and re-raises
        instead.
        """
        row = await self._load(caller, request_id)
        if row.requester_id != caller.employee_id:
            raise AuthorizationError("Only the person who raised it can edit it.")
        editable = {
            MeetingStatus.SUBMITTED,
            MeetingStatus.UNDER_REVIEW,
            MeetingStatus.NEEDS_INFORMATION,
        }
        if row.status not in editable:
            raise ValidationError("This request can no longer be edited.")

        config = await self._settings.get_or_create()
        if payload.preferred_start_at is not None:
            self._check_timing(payload.preferred_start_at, config)

        for field in (
            "title",
            "category",
            "purpose",
            "agenda",
            "expected_outcome",
            "priority",
            "duration_minutes",
            "preferred_start_at",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value.strip() if isinstance(value, str) else value)

        if payload.participant_ids is not None:
            await self._requests.replace_participants(
                row,
                await self._resolve_participants(
                    payload.participant_ids, caller.employee_id, config.ceo_employee_id
                ),
            )

        # Answering the office's question puts it back in their queue, and clears
        # the question so a stale "we need X" does not linger on a fixed request.
        # The preferred time lives in two places - on the request, and as the
        # REQUESTER_PREFERRED slot that approvals actually book. Letting them
        # drift meant answering "can you do next week?" moved the request but
        # left the bookable slot on the old date.
        if payload.preferred_start_at is not None or payload.duration_minutes is not None:
            await self._sync_preferred_slot(row)

        resubmitted = row.status is MeetingStatus.NEEDS_INFORMATION
        if resubmitted:
            row.info_request_note = None
            row.status = MeetingStatus.SUBMITTED
        await self._requests.log(
            request_id=row.id,
            actor_id=caller.employee_id,
            action="resubmitted" if resubmitted else "edited",
        )
        await self._requests.flush()
        if resubmitted:
            await self._alert_founders_office(row, caller.employee_id)
        elif row.status is MeetingStatus.UNDER_REVIEW:
            # Somebody is reading this right now. An edit that arrives silently
            # means they decide on a version that no longer exists.
            await self._alert_founders_office(
                row,
                caller.employee_id,
                title=f"Request updated while under review: {self._subject(row)}",
            )
        return row

    async def _sync_preferred_slot(self, row: MeetingRequest) -> None:
        """Keep the requester's preferred slot in step with the request itself."""
        preferred = next((s for s in row.slots if s.origin is SlotOrigin.REQUESTER_PREFERRED), None)
        if preferred is None:
            await self._requests.add_slot(
                row,
                starts_at=row.preferred_start_at,
                duration_minutes=row.duration_minutes,
                origin=SlotOrigin.REQUESTER_PREFERRED,
                proposed_by_id=row.requester_id,
            )
            return
        preferred.starts_at = row.preferred_start_at
        preferred.duration_minutes = row.duration_minutes
        await self._requests.flush()

    # ---- Founder's Office ------------------------------------------------- #
    async def founders_office_decide(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: FoundersOfficeDecision
    ) -> MeetingRequest:
        await self._require_founders_office(caller)
        row = await self._load(caller, request_id)
        if row.status not in FOUNDERS_OFFICE_ACTIONABLE:
            raise ValidationError("This request is not waiting on the Founder's Office.")
        config = await self._settings.get_or_create()

        if payload.action is FoundersOfficeAction.REQUEST_INFO:
            return await self._send_back(row, caller, payload.note or "")
        if payload.action is FoundersOfficeAction.REJECT:
            return await self._reject(row, caller, ApprovalStage.FOUNDERS_OFFICE, payload.note)
        if payload.action is FoundersOfficeAction.PROPOSE_SLOT:
            assert payload.slot is not None  # the schema validator guarantees it
            return await self._propose(
                row, caller, ApprovalStage.FOUNDERS_OFFICE, payload.slot, payload.note, config
            )
        if payload.action is FoundersOfficeAction.FORWARD:
            return await self._forward(row, caller.employee_id, payload.note)
        return await self._confirm_as_office(row, caller, payload, config)

    async def _confirm_as_office(
        self,
        row: MeetingRequest,
        caller: CurrentUser,
        payload: FoundersOfficeDecision,
        config: MeetingSettings,
    ) -> MeetingRequest:
        """The Founder's Office booking a request itself.

        Permitted only when the workspace allows it and the category is not one
        the CEO reserved. Refusing with a reason (rather than hiding the button)
        matters because the office is the one that has to act next: "forward
        this instead" is actionable, a missing control is not.
        """
        if not config.founders_office_may_confirm:
            raise ValidationError(
                "This workspace requires the CEO to approve every meeting - forward it instead."
            )
        if row.category.value in set(config.categories_requiring_ceo):
            raise ValidationError("The CEO decides this category personally - forward it instead.")
        slot = self._bookable_slot(row, payload.slot_id)
        await self._record(
            row,
            caller.employee_id,
            ApprovalStage.FOUNDERS_OFFICE,
            ApprovalDecision.APPROVED,
            payload.note,
        )
        return await self._book(row, slot, caller.employee_id)

    # ---- CEO -------------------------------------------------------------- #
    async def ceo_decide(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: CeoDecision
    ) -> MeetingRequest:
        config = await self._require_ceo(caller)
        row = await self._load(caller, request_id)
        actionable = {
            MeetingStatus.AWAITING_CEO_APPROVAL,
            MeetingStatus.SLOT_ACCEPTED,
            MeetingStatus.SCHEDULING_FAILED,
        }
        if row.status not in actionable:
            raise ValidationError("This request is not waiting on you.")

        if payload.action is CeoAction.REJECT:
            return await self._reject(row, caller, ApprovalStage.CEO, payload.note)
        if payload.action is CeoAction.PROPOSE_SLOT:
            assert payload.slot is not None  # the schema validator guarantees it
            return await self._propose(
                row, caller, ApprovalStage.CEO, payload.slot, payload.note, config
            )
        slot = self._bookable_slot(row, payload.slot_id)
        await self._record(
            row, caller.employee_id, ApprovalStage.CEO, ApprovalDecision.APPROVED, payload.note
        )
        return await self._book(row, slot, caller.employee_id)

    async def suggest_slot(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: SuggestSlotPayload
    ) -> MeetingRequest:
        """Offer a different time. Either approver may, so the stage is derived
        from who the caller is rather than asked of them.

        The CEO branch is tried first: when the CEO also holds the Founder's
        Office grant, a proposal they make while a request sits in their own
        queue is theirs, and recording it against the office would misattribute
        the decision in the history.
        """
        config = await self._settings.get_or_create()
        row = await self._load(caller, request_id)
        ceo_actionable = {
            MeetingStatus.AWAITING_CEO_APPROVAL,
            MeetingStatus.SLOT_ACCEPTED,
            MeetingStatus.SCHEDULING_FAILED,
        }
        if config.ceo_employee_id == caller.employee_id and row.status in ceo_actionable:
            return await self._propose(
                row, caller, ApprovalStage.CEO, payload.slot, payload.note, config
            )
        await self._require_founders_office(caller)
        if row.status not in FOUNDERS_OFFICE_ACTIONABLE:
            raise ValidationError("This request is not waiting on the Founder's Office.")
        return await self._propose(
            row, caller, ApprovalStage.FOUNDERS_OFFICE, payload.slot, payload.note, config
        )

    # ---- requester answering a proposal ----------------------------------- #
    async def respond_to_slot(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: SlotResponse
    ) -> MeetingRequest:
        """The consent that stops a meeting being moved underneath somebody."""
        row = await self._load(caller, request_id)
        if row.requester_id != caller.employee_id:
            raise AuthorizationError("Only the person who raised it can answer a proposed time.")
        if row.status is not MeetingStatus.SLOT_PROPOSED:
            raise ValidationError("There is no proposed time to answer.")
        offered = self._open_proposal(row)
        if offered is None:
            raise ValidationError("That proposed time is no longer on the table.")

        now = datetime.now(UTC)
        offered.responded_at = now
        if not payload.accept:
            offered.status = SlotStatus.DECLINED
            # Back to the office: they proposed it, so the next move is theirs.
            row.status = MeetingStatus.UNDER_REVIEW
            await self._requests.log(
                request_id=row.id, actor_id=caller.employee_id, action="slot_declined"
            )
            await self._requests.flush()
            await self._alert_founders_office(
                row,
                caller.employee_id,
                kind=NotificationKind.MEETING_SLOT_ANSWERED,
                title=f"Proposed time refused: {self._subject(row)}",
            )
            return row

        offered.status = SlotStatus.ACCEPTED
        row.status = MeetingStatus.SLOT_ACCEPTED
        await self._requests.log(
            request_id=row.id, actor_id=caller.employee_id, action="slot_accepted"
        )
        await self._requests.flush()

        # Who OFFERED it decides what acceptance means, and whose decision it
        # completes. A time the CEO proposed already carries the CEO's yes; one
        # the office proposed carries theirs, and books now only if they may
        # confirm. Either way the approval belongs to the proposer: the
        # requester accepting a time is consent, not self-approval.
        config = await self._settings.get_or_create()
        proposer = offered.proposed_by_id or caller.employee_id
        if offered.origin is SlotOrigin.CEO:
            await self._record(row, proposer, ApprovalStage.CEO, ApprovalDecision.APPROVED, None)
            return await self._book(row, offered, proposer)
        if config.founders_office_may_confirm:
            await self._record(
                row, proposer, ApprovalStage.FOUNDERS_OFFICE, ApprovalDecision.APPROVED, None
            )
            return await self._book(row, offered, proposer)
        return await self._forward(row, proposer, "The requester accepted the proposed time.")

    # ---- cancellation ----------------------------------------------------- #
    async def cancel(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: MeetingCancel
    ) -> MeetingRequest:
        row = await self._load(caller, request_id)
        viewer = await self._viewer(caller)
        if row.status in TERMINAL_STATUSES:
            raise ValidationError("This request is already closed.")

        is_requester = row.requester_id == caller.employee_id
        if not viewer.sees_all and not is_requester:
            raise AuthorizationError("You cannot cancel this request.")
        if is_requester and not viewer.sees_all and row.status not in CANCELLABLE_BY_REQUESTER:
            # Already on the CEO's calendar: withdrawing becomes a request to the
            # office, so nobody disappears from a calendar unannounced.
            raise ValidationError(
                "This meeting is already scheduled - ask the Founder's Office to cancel it."
            )

        # Remove it from the calendar BEFORE recording the cancellation, so a
        # failure to reach Google leaves the meeting intact in both places
        # rather than cancelled here and still on the CEO's day.
        if row.status is MeetingStatus.CONFIRMED:
            await self._booking.unbook(row, actor_id=caller.employee_id)

        row.status = MeetingStatus.CANCELLED
        row.cancel_reason = payload.reason.strip()
        row.cancelled_by_id = caller.employee_id
        await self._requests.log(
            request_id=row.id,
            actor_id=caller.employee_id,
            action="cancelled",
            detail=payload.reason.strip()[:1000],
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.request_cancelled",
            target=f"{_ENTITY}:{row.id}",
        )
        await self._requests.flush()
        await self._announce(
            row,
            caller.employee_id,
            kind=NotificationKind.MEETING_CANCELLED,
            title=f"Meeting cancelled: {self._subject(row)}",
            body=payload.reason.strip()[:300],
        )
        return row

    # ---- shared transitions ----------------------------------------------- #
    async def _send_back(
        self, row: MeetingRequest, caller: CurrentUser, note: str
    ) -> MeetingRequest:
        row.status = MeetingStatus.NEEDS_INFORMATION
        row.info_request_note = note.strip()[:1000]
        await self._record(
            row,
            caller.employee_id,
            ApprovalStage.FOUNDERS_OFFICE,
            ApprovalDecision.INFO_REQUESTED,
            note,
        )
        await self._notify(
            row,
            recipient_id=row.requester_id,
            actor_id=caller.employee_id,
            kind=NotificationKind.MEETING_INFO_NEEDED,
            title=f"More detail needed: {self._subject(row)}",
            body=note.strip()[:300],
        )
        return row

    async def _reject(
        self,
        row: MeetingRequest,
        caller: CurrentUser,
        stage: ApprovalStage,
        note: str | None,
    ) -> MeetingRequest:
        row.status = MeetingStatus.REJECTED
        await self._record(row, caller.employee_id, stage, ApprovalDecision.REJECTED, note)
        await self._notify(
            row,
            recipient_id=row.requester_id,
            actor_id=caller.employee_id,
            kind=NotificationKind.MEETING_REJECTED,
            title=f"Meeting declined: {self._subject(row)}",
            body=(note or "").strip()[:300] or None,
        )
        return row

    async def _forward(
        self, row: MeetingRequest, decided_by_id: uuid.UUID, note: str | None
    ) -> MeetingRequest:
        config = await self._settings.get_or_create()
        if config.ceo_employee_id is None:
            raise ValidationError("No CEO is configured to forward this to.")
        requester = await self._name(row.requester_id)
        row.status = MeetingStatus.AWAITING_CEO_APPROVAL
        await self._record(
            row,
            decided_by_id,
            ApprovalStage.FOUNDERS_OFFICE,
            ApprovalDecision.FORWARDED,
            note,
        )
        await self._notify(
            row,
            recipient_id=config.ceo_employee_id,
            actor_id=decided_by_id,
            kind=NotificationKind.MEETING_APPROVAL_NEEDED,
            title=f"Approval needed: {self._subject(row)}",
            body=f"{row.duration_minutes} minutes, requested by {requester}.",
        )
        return row

    async def _propose(
        self,
        row: MeetingRequest,
        caller: CurrentUser,
        stage: ApprovalStage,
        slot_wanted: SlotProposal,
        note: str | None,
        config: MeetingSettings,
    ) -> MeetingRequest:
        self._check_timing(slot_wanted.starts_at, config)
        # Close off earlier offers first, so the requester can never accept a
        # time that was withdrawn two proposals ago.
        await self._requests.supersede_open_slots(row)
        origin = SlotOrigin.CEO if stage is ApprovalStage.CEO else SlotOrigin.FOUNDERS_OFFICE
        slot = await self._requests.add_slot(
            row,
            starts_at=slot_wanted.starts_at,
            duration_minutes=slot_wanted.duration_minutes,
            origin=origin,
            proposed_by_id=caller.employee_id,
        )
        row.status = MeetingStatus.SLOT_PROPOSED
        await self._record(
            row, caller.employee_id, stage, ApprovalDecision.SLOT_PROPOSED, note, slot_id=slot.id
        )
        await self._notify(
            row,
            recipient_id=row.requester_id,
            actor_id=caller.employee_id,
            kind=NotificationKind.MEETING_SLOT_PROPOSED,
            title=f"A different time is offered: {self._subject(row)}",
            body="Open Avora Founder Meetings to accept or refuse it.",
        )
        return row

    async def _book(
        self, row: MeetingRequest, slot: MeetingSlot, actor_id: uuid.UUID
    ) -> MeetingRequest:
        """Hand off to the calendar, then tell everyone what happened.

        `book` does not raise on an upstream failure, so this reads the status it
        left behind rather than assuming success - a request that could not be
        booked notifies the office to retry instead of telling the requester
        about a meeting that is not there.
        """
        await self._booking.book(row, slot, actor_id=actor_id)
        if row.status is MeetingStatus.CONFIRMED:
            await self._announce(
                row,
                actor_id,
                kind=NotificationKind.MEETING_CONFIRMED,
                title=f"Meeting confirmed: {self._subject(row)}",
                body=f"{self._when_label(row)}. The invitation is on its way.",
            )
        else:
            await self._alert_founders_office(
                row,
                actor_id,
                kind=NotificationKind.MEETING_SCHEDULING_FAILED,
                title=f"Could not book: {self._subject(row)}",
                level=NotificationLevel.WARNING,
            )
        return row

    def _bookable_slot(self, row: MeetingRequest, slot_id: uuid.UUID | None) -> MeetingSlot:
        """The time an approval is approving.

        The rule that makes this more than a lookup: a time an APPROVER put
        forward is bookable only once the requester has accepted it. Without
        that check, "propose 4pm" followed by "confirm" would move the meeting
        with nobody having agreed to the new time.
        """
        if slot_id is not None:
            slot = self._requests.find_slot(row, slot_id)
            if slot is None:
                raise ValidationError("That time is not on this request.")
        else:
            # An ACCEPTED proposal outranks the original ask. The requester said
            # yes to this specific time, and falling back to what they first
            # asked for would book a meeting nobody agreed to - the exact
            # failure the slot model exists to prevent.
            agreed = [s for s in row.slots if s.status is SlotStatus.ACCEPTED]
            slot = max(agreed, key=lambda s: as_utc(s.created_at), default=None) or next(
                (s for s in row.slots if s.origin is SlotOrigin.REQUESTER_PREFERRED), None
            )
            if slot is None:
                raise ValidationError("This request has no time on it.")
        if slot.status is SlotStatus.DECLINED:
            raise ValidationError("That time was already refused.")
        if (
            slot.origin in (SlotOrigin.FOUNDERS_OFFICE, SlotOrigin.CEO)
            and slot.status is not SlotStatus.ACCEPTED
        ):
            raise ValidationError(
                "The requester has not accepted that time yet - it cannot be booked."
            )
        return slot

    def _open_proposal(self, row: MeetingRequest) -> MeetingSlot | None:
        """The newest outstanding offer from an approver."""
        offers = [
            s
            for s in row.slots
            if s.status is SlotStatus.OPEN
            and s.origin in (SlotOrigin.FOUNDERS_OFFICE, SlotOrigin.CEO)
        ]
        return max(offers, key=lambda s: as_utc(s.created_at), default=None)

    async def _record(
        self,
        row: MeetingRequest,
        decided_by_id: uuid.UUID,
        stage: ApprovalStage,
        decision: ApprovalDecision,
        note: str | None,
        *,
        slot_id: uuid.UUID | None = None,
    ) -> None:
        """Append to the decision history and flush both it and the status.

        Takes the DECIDER's id rather than the caller, because the two are not
        always the same person. When a requester accepts a time the office
        proposed, the office's approval is what is being completed - recording
        the requester there would have the history say they approved their own
        meeting.
        """
        await self._requests.append_approval(
            request_id=row.id,
            stage=stage,
            decision=decision,
            decided_by_id=decided_by_id,
            decided_at=datetime.now(UTC),
            note=(note or "").strip()[:1000] or None,
            proposed_slot_id=slot_id,
        )
        await self._audit.append(
            actor=str(decided_by_id),
            action=f"meeting.{stage.value}_{decision.value}",
            target=f"{_ENTITY}:{row.id}",
        )
        await self._requests.flush()

    # ---- notifications ---------------------------------------------------- #
    @staticmethod
    def _subject(row: MeetingRequest) -> str:
        """What a request may be CALLED in a notification.

        A Personal / Confidential request keeps its title out of every inbox
        preview: the person who raised it chose that category precisely so its
        subject would not be visible to anyone but the people deciding it.
        """
        if row.category is MeetingCategory.PERSONAL_CONFIDENTIAL:
            return "a confidential request"
        return row.title

    @staticmethod
    def _when_label(row: MeetingRequest) -> str:
        """The agreed time in the request's own zone, for a person to read.

        Rendered in the zone the request was composed in rather than UTC: a
        notification that says 05:30 for an 11:00 meeting is worse than one
        that gives no time at all.
        """
        if row.scheduled_start_at is None:
            return ""
        zone: tzinfo
        try:
            zone = ZoneInfo(row.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = UTC
        local = as_utc(row.scheduled_start_at).astimezone(zone)
        return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"

    async def _name(self, employee_id: uuid.UUID) -> str:
        employee = await self._employees.get(employee_id)
        return employee.full_name if employee else "someone"

    async def _notify(
        self,
        row: MeetingRequest,
        *,
        recipient_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        kind: NotificationKind,
        title: str,
        body: str | None = None,
        level: NotificationLevel = NotificationLevel.INFO,
    ) -> None:
        # No `link`: the Avora web app has no meetings screen - the extension is
        # the UI - so the entity pair is what routes a click there instead.
        await self._notifications.notify(
            recipient_id=recipient_id,
            kind=kind,
            title=title[:160],
            body=body[:500] if body else None,
            level=level,
            entity_type=_ENTITY,
            entity_id=row.id,
            actor_id=actor_id,
        )

    async def _alert_founders_office(
        self,
        row: MeetingRequest,
        actor_id: uuid.UUID,
        *,
        kind: NotificationKind = NotificationKind.MEETING_REQUEST,
        title: str | None = None,
        level: NotificationLevel = NotificationLevel.INFO,
    ) -> None:
        office = await self._employees.list_founders_office()
        for member in office:
            await self._notify(
                row,
                recipient_id=member.id,
                actor_id=actor_id,
                kind=kind,
                title=title or f"New meeting request: {self._subject(row)}",
                body=f"{row.duration_minutes} minutes, {row.priority.value} priority.",
                level=level,
            )

    async def _announce(
        self,
        row: MeetingRequest,
        actor_id: uuid.UUID,
        *,
        kind: NotificationKind,
        title: str,
        body: str | None = None,
    ) -> None:
        """Tell everyone attached to the meeting: requester, room, office, CEO."""
        config = await self._settings.get_or_create()
        recipients = {row.requester_id, *(p.employee_id for p in row.participants)}
        recipients.update(m.id for m in await self._employees.list_founders_office())
        if config.ceo_employee_id is not None:
            recipients.add(config.ceo_employee_id)
        for recipient_id in recipients:
            await self._notify(
                row,
                recipient_id=recipient_id,
                actor_id=actor_id,
                kind=kind,
                title=title,
                body=body,
            )

    # ---- attachments ------------------------------------------------------ #
    async def _attachable(self, caller: CurrentUser, request_id: uuid.UUID) -> MeetingRequest:
        """The requester's own request, while its papers can still change.

        Frozen once it is with the CEO or on a calendar: an approver who has
        read a deck must not have a different one swapped under them, and a
        confirmed meeting's attachments are part of what was agreed.
        """
        row = await self._load(caller, request_id)
        if row.requester_id != caller.employee_id:
            raise AuthorizationError("Only the person who raised it can change its files.")
        editable = {
            MeetingStatus.SUBMITTED,
            MeetingStatus.UNDER_REVIEW,
            MeetingStatus.NEEDS_INFORMATION,
        }
        if row.status not in editable:
            raise ValidationError("This request's files can no longer be changed.")
        return row

    async def add_attachment(
        self,
        caller: CurrentUser,
        request_id: uuid.UUID,
        data: bytes,
        *,
        label: str | None,
        filename: str | None,
    ) -> MeetingRequest:
        """Attach one document to your own request.

        The browser's Content-Type is deliberately not a parameter: it is a
        claim, wrong in both directions (real PDFs arrive as
        application/octet-stream, and anything can call itself a PDF), so the
        type is decided from the bytes.
        """
        row = await self._attachable(caller, request_id)
        if len(row.attachments) >= MAX_ATTACHMENTS:
            raise ValidationError(
                f"A request can carry at most {MAX_ATTACHMENTS} files. "
                "Remove one before adding another."
            )
        if not data:
            raise ValidationError("The file is empty.")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValidationError("That file is too large (max 10 MB).")
        media_type = detect_media_type(data)
        if media_type is None or media_type not in ALLOWED_ATTACHMENT_TYPES:
            raise ValidationError("Files must be a PDF, PNG, JPG or WebP.")

        object_key: str | None = None
        stored: bytes | None = data
        if self._app_settings.s3_enabled:
            object_key = storage.workspace_object_key(uuid.uuid4().hex, filename)
            try:
                await storage.put_object(object_key, data, media_type)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("meeting_attachment_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        await self._requests.add_attachment(
            row,
            label=_attachment_label(label, filename),
            filename=filename,
            content_type=media_type,
            size_bytes=len(data),
            object_key=object_key,
            content=stored,
            uploaded_by_id=caller.employee_id,
        )
        await self._requests.log(
            request_id=row.id, actor_id=caller.employee_id, action="attachment_added"
        )
        await self._requests.flush()
        return row

    async def get_attachment(
        self, caller: CurrentUser, request_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> MeetingAttachment:
        """The document whose bytes the download endpoint will stream.

        Scope is the request's own, so an approver can read what they are
        deciding on. 404 when out of scope, when the request has no such file,
        or when the id belongs to another request - neither existence nor
        absence leaks.
        """
        row = await self._load(caller, request_id)
        attachment = self._requests.find_attachment(row, attachment_id)
        if attachment is None:
            raise NotFoundError()
        return attachment

    async def remove_attachment(
        self, caller: CurrentUser, request_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> MeetingRequest:
        row = await self._attachable(caller, request_id)
        attachment = self._requests.find_attachment(row, attachment_id)
        if attachment is None:
            raise NotFoundError()
        await self._requests.delete_attachment(row, attachment)
        await self._requests.log(
            request_id=row.id, actor_id=caller.employee_id, action="attachment_removed"
        )
        await self._requests.flush()
        return row

    # ---- read models ------------------------------------------------------ #
    async def to_reads(
        self, caller: CurrentUser, rows: Sequence[MeetingRequest]
    ) -> list[MeetingRequestRead]:
        viewer = await self._viewer(caller)
        names = await self._names_for(rows)
        return [self._read(row, viewer, names) for row in rows]

    async def to_detail(self, caller: CurrentUser, row: MeetingRequest) -> MeetingRequestDetail:
        viewer = await self._viewer(caller)
        approvals = await self._requests.list_approvals(row.id)
        activity = await self._requests.list_activity(row.id)
        event = await self._requests.get_event(row.id)

        wanted = {row.requester_id, *(p.employee_id for p in row.participants)}
        wanted.update(a.decided_by_id for a in approvals if a.decided_by_id)
        people = await self._employees.get_many([i for i in wanted if i])
        names = {i: p.full_name for i, p in people.items()}

        base = self._read(row, viewer, names)
        return MeetingRequestDetail(
            **base.model_dump(),
            purpose=row.purpose,
            agenda=row.agenda,
            expected_outcome=row.expected_outcome,
            info_request_note=row.info_request_note,
            cancel_reason=row.cancel_reason,
            participants=[
                MeetingParticipantRead(
                    employee_id=p.employee_id, full_name=names.get(p.employee_id)
                )
                for p in row.participants
            ],
            slots=[MeetingSlotRead.model_validate(s) for s in row.slots],
            attachments=[MeetingAttachmentRead.model_validate(a) for a in row.attachments],
            approvals=[self._approval_read(a, names) for a in approvals],
            activity=[MeetingActivityRead.model_validate(a) for a in activity],
            event=MeetingEventRead.model_validate(event) if event else None,
        )

    async def _names_for(self, rows: Sequence[MeetingRequest]) -> dict[uuid.UUID, str]:
        """One lookup for every requester on the page, not one per row."""
        people = await self._employees.get_many(
            list({row.requester_id for row in rows}) if rows else []
        )
        return {i: p.full_name for i, p in people.items()}

    def _read(
        self,
        row: MeetingRequest,
        viewer: MeetingViewer,
        names: dict[uuid.UUID, str],
    ) -> MeetingRequestRead:
        is_requester = row.requester_id == viewer.employee_id
        return MeetingRequestRead(
            id=row.id,
            requester_id=row.requester_id,
            requester_name=names.get(row.requester_id),
            title=row.title,
            category=row.category,
            priority=row.priority,
            status=row.status,
            duration_minutes=row.duration_minutes,
            preferred_start_at=row.preferred_start_at,
            scheduled_start_at=row.scheduled_start_at,
            scheduled_end_at=row.scheduled_end_at,
            timezone=row.timezone,
            participant_count=len(row.participants),
            attachment_count=len(row.attachments),
            created_at=row.created_at,
            updated_at=row.updated_at,
            can_decide=self._can_decide(row, viewer),
            can_cancel=(
                row.status not in TERMINAL_STATUSES
                and (viewer.sees_all or (is_requester and row.status in CANCELLABLE_BY_REQUESTER))
            ),
            can_respond_to_slot=is_requester and row.status is MeetingStatus.SLOT_PROPOSED,
        )

    @staticmethod
    def _can_decide(row: MeetingRequest, viewer: MeetingViewer) -> bool:
        """Whether THIS viewer can act on it now, from the same rules the
        decision endpoints enforce - so a button never appears for an action the
        server would refuse."""
        if viewer.is_founders_office and row.status in FOUNDERS_OFFICE_ACTIONABLE:
            return True
        return viewer.is_ceo and row.status is MeetingStatus.AWAITING_CEO_APPROVAL

    @staticmethod
    def _approval_read(
        approval: MeetingApproval, names: dict[uuid.UUID, str]
    ) -> MeetingApprovalRead:
        read = MeetingApprovalRead.model_validate(approval)
        if approval.decided_by_id is not None:
            return read.model_copy(update={"decided_by_name": names.get(approval.decided_by_id)})
        return read

    # ---- settings --------------------------------------------------------- #
    async def read_settings(self, caller: CurrentUser) -> MeetingSettingsRead:
        """Who may read the configuration: the office, the CEO, and admins.

        An ordinary employee does not need it - the submit form learns the
        notice window from the error it gets back - and the CEO's calendar id is
        not something to hand out.
        """
        config = await self._settings.get_or_create()
        if not (
            caller.founders_office
            or caller.is_admin
            or config.ceo_employee_id == caller.employee_id
        ):
            raise AuthorizationError("You cannot read the meeting settings.")
        ceo_name = await self._name(config.ceo_employee_id) if config.ceo_employee_id else None
        read = MeetingSettingsRead.model_validate(config)
        return read.model_copy(
            update={
                "ceo_name": ceo_name,
                "configured": config.ceo_employee_id is not None
                and self._booking.calendar_configured,
            }
        )

    async def update_settings(
        self, caller: CurrentUser, payload: MeetingSettingsUpdate
    ) -> MeetingSettingsRead:
        # Naming the CEO and granting the office are privilege changes, so this
        # is admin-only - not the Founder's Office, who would otherwise be able
        # to appoint their own approver (rule 5.5).
        if not caller.is_admin:
            raise AuthorizationError("Only an admin can change the meeting settings.")
        config = await self._settings.get_or_create()

        if payload.ceo_employee_id is not None:
            ceo = await self._employees.get(payload.ceo_employee_id)
            if ceo is None or not ceo.is_active:
                raise ValidationError("That person is not an active employee.")
            config.ceo_employee_id = ceo.id
        for field in (
            "ceo_calendar_id",
            "founders_office_may_confirm",
            "timezone",
            "buffer_minutes",
            "booking_window_days",
            "min_notice_hours",
            "reminder_lead_minutes",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(config, field, value)
        if payload.categories_requiring_ceo is not None:
            config.categories_requiring_ceo = [c.value for c in payload.categories_requiring_ceo]

        config.updated_by = caller.employee_id
        await self._settings.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.settings_changed",
            target="meeting_settings",
        )
        return await self.read_settings(caller)


def _attachment_label(label: str | None, filename: str | None) -> str:
    """What the reviewer sees in the list.

    Falls back to the filename so a document is never nameless, and to a plain
    word when even that is missing (some clients send no filename at all).
    """
    chosen = (label or "").strip() or (filename or "").strip()
    return chosen[:120] or "Attachment"
