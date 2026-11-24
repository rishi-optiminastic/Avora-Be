"""Founder Meetings - the decision history, kept apart from the request status.

Why its own table rather than the reviewer trio bolted onto the request (the
shape reimbursements and resignations use): a meeting request can be decided
more than once. The Founder's Office may send it back for information, see it
again, propose a time, and only then forward it; the CEO may counter-propose
before approving. Columns on the request would keep only the last of those, and
"who approved this" is exactly the question that must survive a later reschedule.

So `meeting_approvals` is APPEND-ONLY. Every act of judgement appends a row and
nothing ever updates one. `meeting_requests.status` is a cache of where that
history has arrived, derived on each transition, and the rows here are the
record (Security rule 5.7).

`meeting_activity_logs` sits alongside it for everything that is NOT a decision:
submitted, edited, attachment added, calendar write failed, reminder sent. Two
tables because they answer different questions and one of them is evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApprovalStage(StrEnum):
    FOUNDERS_OFFICE = "founders_office"
    CEO = "ceo"


class ApprovalDecision(StrEnum):
    """What a reviewer actually did.

    APPROVED and FORWARDED are separate on purpose. Both are the Founder's
    Office saying yes, but one books the meeting and the other hands the
    judgement to the CEO - and an office that forwards everything tells you
    something different about the workload than one that confirms everything.
    """

    APPROVED = "approved"  # yes, and this stage is the last word: schedule it
    FORWARDED = "forwarded"  # yes, but the CEO decides (Founder's Office only)
    REJECTED = "rejected"
    INFO_REQUESTED = "info_requested"  # sent back to the requester with a question
    SLOT_PROPOSED = "slot_proposed"  # a different time offered, awaiting the requester


class MeetingApproval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_approvals"
    __table_args__ = (
        # The detail panel's history, and "has the CEO already seen this".
        Index("ix_meeting_approvals_request_stage", "meeting_request_id", "stage"),
    )

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[ApprovalStage] = mapped_column()
    decision: Mapped[ApprovalDecision] = mapped_column()
    # Nullable and SET NULL: a decision outlives the decider's employment, and
    # losing the row would rewrite history. The name is resolved for display at
    # read time, so an offboarded approver still reads as a person.
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    note: Mapped[str | None] = mapped_column(String(1000), default=None)
    # The slot offered, when the decision was SLOT_PROPOSED. Lets the history
    # read "offered Tue 11:00" instead of just "proposed another time".
    proposed_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_slots.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MeetingActivityLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Everything that happened to a request that was not a decision.

    Deliberately a free-text `action` slug rather than an enum: this is a
    diary for humans reading one request's life, and a new kind of event
    (a reminder, a reconciliation correction) must not need a migration to be
    writable. Decisions are never written here - they go to `meeting_approvals`,
    which is the table with evidentiary weight.
    """

    __tablename__ = "meeting_activity_logs"
    __table_args__ = (
        Index("ix_meeting_activity_logs_request_created", "meeting_request_id", "created_at"),
    )

    meeting_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_requests.id", ondelete="CASCADE"), index=True
    )
    # Null for anything the system did on its own (a scheduler completing a past
    # meeting, a reconciliation pass correcting a drifted event).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(60))
    # One sentence for a human. Never the agenda or an attachment's contents:
    # this is read by the Founder's Office on every request, including the
    # confidential ones (Logging rules - no sensitive content in diaries).
    detail: Mapped[str | None] = mapped_column(String(1000), default=None)
