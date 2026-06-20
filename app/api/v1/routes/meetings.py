"""Quick Meet — any authenticated employee starts an instant meeting.

One POST creates a Google Meet link, posts it to the team Slack channel, and
returns the link for the caller to join. No role gate: starting a meeting is open
to everyone (the audit log records who did).
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, MeetingServiceDep
from app.schemas.meeting import QuickMeetingRead

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("/quick", response_model=QuickMeetingRead, status_code=status.HTTP_201_CREATED)
async def start_quick_meeting(
    caller: CurrentUserDep, service: MeetingServiceDep
) -> QuickMeetingRead:
    return await service.start_quick(caller)
