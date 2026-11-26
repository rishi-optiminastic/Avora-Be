"""Founder Meetings data access - the only place meeting queries are built.

Row-level scope (Security rule 5.3) is decided once, in `_scope_clause`, from a
`MeetingViewer` the service builds the same way every time. Three readers exist:

* the requester, who sees their own requests;
* a participant, who sees the ones they were put in the room for;
* the Founder's Office and the CEO, who see everything.

Admin is deliberately NOT a reader here, by the same reasoning that keeps admins
out of reimbursements: running the workspace is not a reason to read what
somebody wanted to tell the founder privately. An admin configures the feature
(names the CEO, grants the office) and sees nothing else. Note this also covers
`it_admin`, which normalises to ADMIN on `CurrentUser`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting_approval import (
    ApprovalDecision,
    ApprovalStage,
    MeetingActivityLog,
    MeetingApproval,
)
from app.models.meeting_event import EventSyncStatus, MeetingEvent
from app.models.meeting_request import (
    MeetingAttachment,
    MeetingParticipant,
    MeetingRequest,
    MeetingSlot,
    MeetingStatus,
    SlotOrigin,
    SlotStatus,
)


@dataclass(frozen=True)
class MeetingViewer:
    """Who is looking, reduced to what scoping actually needs.

    Built in exactly one place (`MeetingRequestService._viewer`), because
    "is this person the CEO" is a settings lookup rather than a token claim and
    must not be re-answered ad hoc at each call site.
    """

    employee_id: uuid.UUID
    is_founders_office: bool = False
    is_ceo: bool = False

    @property
    def sees_all(self) -> bool:
        return self.is_founders_office or self.is_ceo


class MeetingRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- scope ------------------------------------------------------------ #
    def _scope_clause(self, viewer: MeetingViewer) -> ColumnElement[bool] | None:
        """None means "no restriction"; otherwise the WHERE every read gets."""
        if viewer.sees_all:
            return None
        in_the_room = select(MeetingParticipant.meeting_request_id).where(
            MeetingParticipant.employee_id == viewer.employee_id
        )
        return (MeetingRequest.requester_id == viewer.employee_id) | MeetingRequest.id.in_(
            in_the_room
        )

    # ---- requests --------------------------------------------------------- #
    async def create(
        self,
        *,
        requester_id: uuid.UUID,
        fields: dict[str, object],
    ) -> MeetingRequest:
        """Insert a request. `fields` carries only validated, server-chosen
        values - the caller never passes a status or a scheduled time."""
        row = MeetingRequest(
            requester_id=requester_id,
            status=MeetingStatus.SUBMITTED,
            # Set explicitly so the selectin relationships count as loaded;
            # otherwise serializing the response would lazy-load outside the
            # async context and blow up (the trap reimbursements documents).
            participants=[],
            slots=[],
            attachments=[],
            **fields,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, request_id: uuid.UUID) -> MeetingRequest | None:
        return await self._session.get(MeetingRequest, request_id)

    async def get_in_scope(
        self, viewer: MeetingViewer, request_id: uuid.UUID
    ) -> MeetingRequest | None:
        clause = self._scope_clause(viewer)
        stmt = select(MeetingRequest).where(MeetingRequest.id == request_id)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_scope(
        self,
        viewer: MeetingViewer,
        *,
        offset: int,
        limit: int,
        status: MeetingStatus | None = None,
        statuses: Sequence[MeetingStatus] | None = None,
        mine_only: bool = False,
        scheduled_from: datetime | None = None,
        scheduled_to: datetime | None = None,
    ) -> tuple[Sequence[MeetingRequest], int]:
        stmt = select(MeetingRequest)
        clause = self._scope_clause(viewer)
        if clause is not None:
            stmt = stmt.where(clause)
        if mine_only:
            # The Founder's Office asking for "my own requests" - they see
            # everything, so without this their list is the whole queue.
            stmt = stmt.where(MeetingRequest.requester_id == viewer.employee_id)
        if status is not None:
            stmt = stmt.where(MeetingRequest.status == status)
        if statuses:
            stmt = stmt.where(MeetingRequest.status.in_(list(statuses)))
        if scheduled_from is not None:
            stmt = stmt.where(MeetingRequest.scheduled_start_at >= scheduled_from)
        if scheduled_to is not None:
            stmt = stmt.where(MeetingRequest.scheduled_start_at < scheduled_to)

        total = await self._session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
        # Urgent first, then by the time being asked for. A queue ordered by
        # creation makes the office work oldest-first on a list where one row is
        # a client escalation tomorrow and another is a catch-up next month.
        rows = await self._session.execute(
            stmt.order_by(
                MeetingRequest.priority.desc(),
                MeetingRequest.preferred_start_at.asc(),
                MeetingRequest.created_at.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def count_by_status(self, viewer: MeetingViewer) -> dict[MeetingStatus, int]:
        """Every status this viewer can see, counted in one query - the popup
        badge would otherwise be five list calls on each open."""
        stmt = select(MeetingRequest.status, func.count()).group_by(MeetingRequest.status)
        clause = self._scope_clause(viewer)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = await self._session.execute(stmt)
        return {MeetingStatus(status): int(count) for status, count in rows.all()}

    async def count_own_open(self, employee_id: uuid.UUID, open_statuses: Sequence[str]) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(MeetingRequest)
            .where(
                MeetingRequest.requester_id == employee_id,
                MeetingRequest.status.in_(list(open_statuses)),
            )
        )
        return int(total or 0)

    async def due_for_completion(self, *, before: datetime, limit: int) -> Sequence[MeetingRequest]:
        """Confirmed meetings whose end time has passed - the worker marks these
        COMPLETED. Not scope-clamped: its only caller is a background job with
        no human principal to scope to."""
        rows = await self._session.execute(
            select(MeetingRequest)
            .where(
                MeetingRequest.status == MeetingStatus.CONFIRMED,
                MeetingRequest.scheduled_end_at.is_not(None),
                MeetingRequest.scheduled_end_at < before,
            )
            .order_by(MeetingRequest.scheduled_end_at.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def due_for_reminder(
        self, *, now: datetime, until: datetime, limit: int
    ) -> Sequence[MeetingRequest]:
        """Confirmed meetings starting soon that nobody has been nudged about.

        `reminder_sent_at IS NULL` is what makes this safe to run every few
        minutes: once the worker stamps a row it drops out of the window.

        Bounded BELOW by `now` as well as above. A worker that was down over
        the weekend must not come back and announce Monday's meetings as
        starting soon when they finished on Friday - the row would still be
        CONFIRMED (nothing marked it complete while the worker was dead) and
        would otherwise match on the upper bound alone.

        Not scope-clamped: its only caller is a background job with no human
        principal to scope to.
        """
        rows = await self._session.execute(
            select(MeetingRequest)
            .where(
                MeetingRequest.status == MeetingStatus.CONFIRMED,
                MeetingRequest.reminder_sent_at.is_(None),
                MeetingRequest.scheduled_start_at.is_not(None),
                MeetingRequest.scheduled_start_at >= now,
                MeetingRequest.scheduled_start_at <= until,
            )
            .order_by(MeetingRequest.scheduled_start_at.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def stuck_scheduling(self, *, limit: int) -> Sequence[MeetingRequest]:
        """Approved meetings the calendar would not take.

        Both approvers already said yes to these; only Google refused. They are
        the one state in this feature that nothing recovers on its own, because
        no invitation exists to remind anybody they were supposed to happen.

        Oldest first, so a backlog is cleared in the order it accumulated.
        """
        rows = await self._session.execute(
            select(MeetingRequest)
            .where(MeetingRequest.status == MeetingStatus.SCHEDULING_FAILED)
            .order_by(MeetingRequest.updated_at.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    # ---- participants ----------------------------------------------------- #
    async def replace_participants(
        self, row: MeetingRequest, employee_ids: Sequence[uuid.UUID]
    ) -> None:
        """Set the room to exactly these people.

        A replace rather than a merge: the edit form sends the list it wants,
        and a merge would make removing somebody impossible.
        """
        for existing in list(row.participants):
            await self._session.delete(existing)
        await self._session.flush()
        for employee_id in dict.fromkeys(employee_ids):
            self._session.add(
                MeetingParticipant(meeting_request_id=row.id, employee_id=employee_id)
            )
        await self._session.flush()
        await self._session.refresh(row, ["participants"])

    # ---- slots ------------------------------------------------------------ #
    async def add_slot(
        self,
        row: MeetingRequest,
        *,
        starts_at: datetime,
        duration_minutes: int,
        origin: SlotOrigin,
        proposed_by_id: uuid.UUID | None = None,
        status: SlotStatus = SlotStatus.OPEN,
    ) -> MeetingSlot:
        slot = MeetingSlot(
            meeting_request_id=row.id,
            starts_at=starts_at,
            duration_minutes=duration_minutes,
            origin=origin,
            proposed_by_id=proposed_by_id,
            status=status,
        )
        self._session.add(slot)
        await self._session.flush()
        await self._session.refresh(row, ["slots"])
        return slot

    def find_slot(self, row: MeetingRequest, slot_id: uuid.UUID) -> MeetingSlot | None:
        """One slot, but only if it belongs to this request - the request is what
        was scope-checked, so a slot id from another must not resolve here."""
        return next((s for s in row.slots if s.id == slot_id), None)

    async def supersede_open_slots(self, row: MeetingRequest) -> None:
        """Close off every outstanding offer before a new one is made, so a
        requester can never accept a time that was withdrawn two proposals ago."""
        for slot in row.slots:
            if slot.status is SlotStatus.OPEN and slot.origin in (
                SlotOrigin.FOUNDERS_OFFICE,
                SlotOrigin.CEO,
            ):
                slot.status = SlotStatus.SUPERSEDED
        await self._session.flush()

    async def close_open_slots(self, row: MeetingRequest, *, keep_id: uuid.UUID) -> None:
        """Take every other offer off the table once one is booked.

        Without this a confirmed meeting still showed an "open" alternative in
        the detail panel, which reads as a time somebody might still take.
        """
        for slot in row.slots:
            if slot.id != keep_id and slot.status is SlotStatus.OPEN:
                slot.status = SlotStatus.SUPERSEDED
        await self._session.flush()

    # ---- approvals (append-only) ------------------------------------------ #
    async def append_approval(
        self,
        *,
        request_id: uuid.UUID,
        stage: ApprovalStage,
        decision: ApprovalDecision,
        decided_by_id: uuid.UUID | None,
        decided_at: datetime,
        note: str | None = None,
        proposed_slot_id: uuid.UUID | None = None,
    ) -> MeetingApproval:
        approval = MeetingApproval(
            meeting_request_id=request_id,
            stage=stage,
            decision=decision,
            decided_by_id=decided_by_id,
            decided_at=decided_at,
            note=note,
            proposed_slot_id=proposed_slot_id,
        )
        self._session.add(approval)
        await self._session.flush()
        return approval

    async def list_approvals(self, request_id: uuid.UUID) -> Sequence[MeetingApproval]:
        rows = await self._session.execute(
            select(MeetingApproval)
            .where(MeetingApproval.meeting_request_id == request_id)
            .order_by(MeetingApproval.decided_at.asc())
        )
        return list(rows.scalars().all())

    # ---- activity diary --------------------------------------------------- #
    async def log(
        self,
        *,
        request_id: uuid.UUID,
        action: str,
        actor_id: uuid.UUID | None = None,
        detail: str | None = None,
    ) -> None:
        self._session.add(
            MeetingActivityLog(
                meeting_request_id=request_id,
                actor_id=actor_id,
                action=action,
                detail=detail,
            )
        )
        await self._session.flush()

    async def list_activity(
        self, request_id: uuid.UUID, *, limit: int = 100
    ) -> Sequence[MeetingActivityLog]:
        rows = await self._session.execute(
            select(MeetingActivityLog)
            .where(MeetingActivityLog.meeting_request_id == request_id)
            .order_by(MeetingActivityLog.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    # ---- calendar event --------------------------------------------------- #
    async def get_event(self, request_id: uuid.UUID) -> MeetingEvent | None:
        rows = await self._session.execute(
            select(MeetingEvent).where(MeetingEvent.meeting_request_id == request_id)
        )
        return rows.scalar_one_or_none()

    async def claim_event(self, request_id: uuid.UUID, *, calendar_id: str) -> MeetingEvent:
        """Reserve the one event row for this request.

        The unique constraint on `meeting_request_id` is what makes two
        simultaneous approvals produce one event instead of two: both reach
        here, one inserts, the other raises IntegrityError and is told the
        meeting is already being scheduled.
        """
        row = MeetingEvent(
            meeting_request_id=request_id,
            google_calendar_id=calendar_id,
            sync_status=EventSyncStatus.PENDING,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    # ---- attachments ------------------------------------------------------ #
    async def add_attachment(
        self,
        row: MeetingRequest,
        *,
        label: str,
        filename: str | None,
        content_type: str,
        size_bytes: int,
        object_key: str | None,
        content: bytes | None,
        uploaded_by_id: uuid.UUID,
    ) -> MeetingAttachment:
        """Append one document. Exactly one of `object_key`/`content` is set by
        the caller, mirroring how reimbursement receipts store."""
        attachment = MeetingAttachment(
            meeting_request_id=row.id,
            label=label,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            object_key=object_key,
            content=content,
            uploaded_by_id=uploaded_by_id,
        )
        self._session.add(attachment)
        await self._session.flush()
        await self._session.refresh(row, ["attachments"])
        return attachment

    def find_attachment(
        self, row: MeetingRequest, attachment_id: uuid.UUID
    ) -> MeetingAttachment | None:
        return next((a for a in row.attachments if a.id == attachment_id), None)

    async def delete_attachment(self, row: MeetingRequest, attachment: MeetingAttachment) -> None:
        await self._session.delete(attachment)
        await self._session.flush()
        await self._session.refresh(row, ["attachments"])

    async def flush(self) -> None:
        await self._session.flush()
