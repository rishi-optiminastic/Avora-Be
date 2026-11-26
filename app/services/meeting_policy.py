"""Which time a meeting gets booked at.

One function, in its own module, because getting it wrong is not a small bug:
an earlier version of this rule ignored accepted proposals, so a request where
the office offered 3 December and the requester accepted it was booked on 27
November - the date originally asked for. Two copies of a rule like that is one
copy too many, and the worker that retries a failed booking needs exactly the
same answer the approval endpoint reached.
"""

from __future__ import annotations

from app.core.time import as_utc
from app.models.meeting_request import MeetingRequest, MeetingSlot, SlotOrigin, SlotStatus


def default_slot(row: MeetingRequest) -> MeetingSlot | None:
    """The time to book when nobody named one, or None if there is none.

    An ACCEPTED proposal outranks the original ask. The requester said yes to
    that specific time, and falling back to what they first asked for would
    book a meeting nobody agreed to - the exact failure the slot model exists
    to prevent. The newest accepted one wins, because a later agreement
    replaces an earlier one.
    """
    agreed = [slot for slot in row.slots if slot.status is SlotStatus.ACCEPTED]
    newest = max(agreed, key=lambda slot: as_utc(slot.created_at), default=None)
    if newest is not None:
        return newest
    return next((slot for slot in row.slots if slot.origin is SlotOrigin.REQUESTER_PREFERRED), None)


def refusal_reason(slot: MeetingSlot) -> str | None:
    """Why this time cannot be booked, or None when it can.

    The rule that makes this more than a lookup: a time an APPROVER put forward
    is bookable only once the requester has accepted it. Without that check,
    "propose 4pm" followed by "confirm" would move the meeting with nobody
    having agreed to the new time.
    """
    if slot.status is SlotStatus.DECLINED:
        return "That time was already refused."
    if (
        slot.origin in (SlotOrigin.FOUNDERS_OFFICE, SlotOrigin.CEO)
        and slot.status is not SlotStatus.ACCEPTED
    ):
        return "The requester has not accepted that time yet - it cannot be booked."
    return None
