"""Quick Meet — any authenticated employee starts an instant meeting.

One POST creates a Google Meet link, invites the caller's saved default invitees,
posts it to the team Slack channel, and returns the link to join. No role gate:
starting a meeting is open to everyone (the audit log records who did). The
default-invitee endpoints are strictly self-scoped — a caller only ever reads or
writes their own list (Golden rule #3).
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, MeetingServiceDep
from app.schemas.meeting import QuickMeetDefaultsRead, QuickMeetDefaultsUpdate, QuickMeetingRead

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("/quick", response_model=QuickMeetingRead, status_code=status.HTTP_201_CREATED)
async def start_quick_meeting(
    caller: CurrentUserDep, service: MeetingServiceDep
) -> QuickMeetingRead:
    return await service.start_quick(caller)


@router.get("/quick/defaults", response_model=QuickMeetDefaultsRead)
async def get_quick_defaults(
    caller: CurrentUserDep, service: MeetingServiceDep
) -> QuickMeetDefaultsRead:
    """The caller's own default invitees."""
    return await service.get_defaults(caller)


@router.put("/quick/defaults", response_model=QuickMeetDefaultsRead)
async def set_quick_defaults(
    payload: QuickMeetDefaultsUpdate, caller: CurrentUserDep, service: MeetingServiceDep
) -> QuickMeetDefaultsRead:
    """Replace the caller's own default invitees."""
    return await service.set_defaults(caller, [str(e) for e in payload.invitee_emails])
