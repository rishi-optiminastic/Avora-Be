"""Founder Meetings endpoints - request, triage, decide, book.

Paths live under the existing `/meetings` prefix beside Quick Meet, which owns
`/meetings/quick`. Nothing here is a wildcard at that level, so the two routers
coexist; WITHIN this one, `/requests/counts` is declared before
`/requests/{request_id}` or the literal would be swallowed by the path
parameter.

Named verbs rather than one decision endpoint, matching the documented contract:
each route builds the decision the service expects and calls a single entry
point, so the state machine has one door and the routes stay free of logic
(Layering: route -> service -> repository).

Every route is authorized in the service from the authenticated principal.
There is no role check in this module, and none in the extension either.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from fastapi.responses import RedirectResponse

from app.core import storage
from app.core.deps import (
    CurrentUserDep,
    DownloadRateLimitDep,
    IdempotencyKeyHeader,
    IdempotencyServiceDep,
    MeetingRequestServiceDep,
    UploadRateLimitDep,
)
from app.core.exceptions import NotFoundError
from app.models.meeting_request import MeetingRequest, MeetingStatus
from app.schemas.auth import CurrentUser
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.meeting_request import (
    ApprovePayload,
    CeoDecision,
    DecisionNote,
    FoundersOfficeAction,
    FoundersOfficeDecision,
    MeetingCancel,
    MeetingQueueCounts,
    MeetingRequestCreate,
    MeetingRequestDetail,
    MeetingRequestRead,
    MeetingRequestUpdate,
    MeetingSettingsRead,
    MeetingSettingsUpdate,
    RequiredNote,
    SlotResponse,
    SuggestSlotPayload,
)
from app.services.meeting_request_service import MeetingRequestService

router = APIRouter(prefix="/meetings", tags=["meetings"])


async def _detail(
    service: MeetingRequestService, caller: CurrentUser, row: MeetingRequest
) -> MeetingRequestDetail:
    """Actions return the request as it now stands, history included, so the
    client never has to follow a mutation with a read to redraw."""
    return await service.to_detail(caller, row)


def _safe_filename(name: str) -> str:
    """Strip path separators and quotes so a filename can't break the header."""
    cleaned = name.replace("\\", "/").split("/")[-1].replace('"', "").strip()
    return cleaned or "attachment"


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
@router.get("/requests", response_model=Page[MeetingRequestRead])
async def list_requests(
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status_filter: Annotated[MeetingStatus | None, Query(alias="status")] = None,
    mine: Annotated[bool, Query()] = False,
    scheduled_from: Annotated[datetime | None, Query()] = None,
    scheduled_to: Annotated[datetime | None, Query()] = None,
) -> Page[MeetingRequestRead]:
    """Requests this caller may see.

    `mine=true` narrows to the caller's own, which the Founder's Office needs
    because they see everything by default. `scheduled_from`/`scheduled_to` are
    what the calendar view fetches a month with.
    """
    rows, total = await service.list_for_caller(
        caller,
        offset=(page - 1) * size,
        limit=size,
        status=status_filter,
        mine_only=mine,
        scheduled_from=scheduled_from,
        scheduled_to=scheduled_to,
    )
    return Page(
        items=await service.to_reads(caller, rows),
        page=page,
        size=size,
        total=total,
    )


@router.post("/requests", response_model=MeetingRequestDetail, status_code=status.HTTP_201_CREATED)
async def submit_request(
    payload: MeetingRequestCreate,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Ask for time with the CEO.

    Idempotent on the key: a retrying extension (a service worker that was
    killed mid-request) must not raise the same meeting twice.
    """

    async def _op() -> MeetingRequestDetail:
        row = await service.submit(caller, payload)
        return await _detail(service, caller, row)

    return await idem.run(
        principal_id=caller.employee_id,
        scope="meetings.requests.create",
        key=idempotency_key,
        request=payload.model_dump(mode="json"),
        operation=_op,
        success_status=status.HTTP_201_CREATED,
    )


# Declared BEFORE /requests/{request_id}: a literal after a path parameter is
# unreachable, which is how `/assessments/attempts/me` once became a 422.
@router.get("/requests/counts", response_model=MeetingQueueCounts)
async def request_counts(
    caller: CurrentUserDep, service: MeetingRequestServiceDep
) -> MeetingQueueCounts:
    """The queue sizes the popup badge and the dashboard tiles show."""
    return await service.queue_counts(caller)


@router.get("/requests/{request_id}", response_model=MeetingRequestDetail)
async def get_request(
    request_id: uuid.UUID,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    return await _detail(service, caller, await service.get_for_caller(caller, request_id))


@router.patch("/requests/{request_id}", response_model=MeetingRequestDetail)
async def update_request(
    request_id: uuid.UUID,
    payload: MeetingRequestUpdate,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """The requester correcting their own request, or answering a send-back."""
    row = await service.update(caller, request_id, payload)
    return await _detail(service, caller, row)


# --------------------------------------------------------------------------- #
# Founder's Office
# --------------------------------------------------------------------------- #
@router.post("/requests/{request_id}/review", response_model=MeetingRequestDetail)
async def mark_under_review(
    request_id: uuid.UUID,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """The Founder's Office picking a request up, so a shared queue shows who
    is on what. A no-op on anything already past SUBMITTED."""
    row = await service.mark_under_review(caller, request_id)
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/approve", response_model=MeetingRequestDetail)
async def approve_request(
    request_id: uuid.UUID,
    payload: ApprovePayload,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """The Founder's Office approving AND booking it.

    Refused with a reason when the workspace reserves the decision for the CEO,
    or when the category does - in which case the office forwards instead.
    """
    row = await service.founders_office_decide(
        caller,
        request_id,
        FoundersOfficeDecision(
            action=FoundersOfficeAction.CONFIRM, note=payload.note, slot_id=payload.slot_id
        ),
    )
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/forward", response_model=MeetingRequestDetail)
async def forward_request(
    request_id: uuid.UUID,
    payload: DecisionNote,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """The Founder's Office saying yes but handing the decision to the CEO."""
    row = await service.founders_office_decide(
        caller,
        request_id,
        FoundersOfficeDecision(action=FoundersOfficeAction.FORWARD, note=payload.note),
    )
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/reject", response_model=MeetingRequestDetail)
async def reject_request(
    request_id: uuid.UUID,
    payload: RequiredNote,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """Decline it. The reason is required: the requester reads it."""
    row = await service.founders_office_decide(
        caller,
        request_id,
        FoundersOfficeDecision(action=FoundersOfficeAction.REJECT, note=payload.note),
    )
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/request-info", response_model=MeetingRequestDetail)
async def request_more_information(
    request_id: uuid.UUID,
    payload: RequiredNote,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """Send it back with a question, which the requester answers by editing."""
    row = await service.founders_office_decide(
        caller,
        request_id,
        FoundersOfficeDecision(action=FoundersOfficeAction.REQUEST_INFO, note=payload.note),
    )
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/suggest-slot", response_model=MeetingRequestDetail)
async def suggest_slot(
    request_id: uuid.UUID,
    payload: SuggestSlotPayload,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """Offer a different time. Either approver may; the service works out which
    stage the caller is acting at. The meeting does NOT move until the requester
    accepts it."""
    row = await service.suggest_slot(caller, request_id, payload)
    return await _detail(service, caller, row)


# --------------------------------------------------------------------------- #
# CEO
# --------------------------------------------------------------------------- #
@router.post("/requests/{request_id}/ceo-decision", response_model=MeetingRequestDetail)
async def ceo_decision(
    request_id: uuid.UUID,
    payload: CeoDecision,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """The CEO approving, declining, or offering another time."""
    row = await service.ceo_decide(caller, request_id, payload)
    return await _detail(service, caller, row)


# --------------------------------------------------------------------------- #
# Requester
# --------------------------------------------------------------------------- #
@router.post("/requests/{request_id}/accept-slot", response_model=MeetingRequestDetail)
async def respond_to_slot(
    request_id: uuid.UUID,
    payload: SlotResponse,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """Accept or refuse a time an approver proposed.

    This is the consent that stops a meeting being moved underneath somebody,
    so it is the requester's call and nobody else's.
    """
    row = await service.respond_to_slot(caller, request_id, payload)
    return await _detail(service, caller, row)


@router.post("/requests/{request_id}/cancel", response_model=MeetingRequestDetail)
async def cancel_request(
    request_id: uuid.UUID,
    payload: MeetingCancel,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    """Call it off. The requester may while it is unscheduled; the Founder's
    Office and the CEO may at any point, which also deletes the calendar event
    and tells the attendees."""
    row = await service.cancel(caller, request_id, payload)
    return await _detail(service, caller, row)


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
@router.post(
    "/requests/{request_id}/attachments",
    response_model=MeetingRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
async def add_attachment(
    request_id: uuid.UUID,
    caller: UploadRateLimitDep,
    service: MeetingRequestServiceDep,
    file: Annotated[UploadFile, File()],
    label: Annotated[str | None, Form()] = None,
) -> MeetingRequestDetail:
    """Attach a document to your own request, while it can still be edited.

    PDF, PNG, JPG or WebP, 10 MB each, up to five per request. The type is read
    from the bytes, not from what the browser called it.
    """
    data = await file.read()
    row = await service.add_attachment(
        caller, request_id, data, label=label, filename=file.filename
    )
    return await _detail(service, caller, row)


@router.delete(
    "/requests/{request_id}/attachments/{attachment_id}", response_model=MeetingRequestDetail
)
async def remove_attachment(
    request_id: uuid.UUID,
    attachment_id: uuid.UUID,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingRequestDetail:
    row = await service.remove_attachment(caller, request_id, attachment_id)
    return await _detail(service, caller, row)


@router.get("/requests/{request_id}/attachments/{attachment_id}")
async def download_attachment(
    request_id: uuid.UUID,
    attachment_id: uuid.UUID,
    caller: DownloadRateLimitDep,
    service: MeetingRequestServiceDep,
) -> Response:
    """One document, scoped to whoever may see the request (404 otherwise).

    Served as a neutral-type attachment - an uploaded file is never handed back
    with a type a browser will execute.
    """
    attachment = await service.get_attachment(caller, request_id, attachment_id)
    safe = _safe_filename(attachment.filename or attachment.label or "attachment")
    if attachment.object_key:
        url = storage.presigned_get_url(attachment.object_key, download_filename=safe)
        return RedirectResponse(url, status_code=307)
    if attachment.content is not None:
        return Response(
            content=attachment.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )
    raise NotFoundError()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@router.get("/settings", response_model=MeetingSettingsRead)
async def read_meeting_settings(
    caller: CurrentUserDep, service: MeetingRequestServiceDep
) -> MeetingSettingsRead:
    """How Founder Meetings is configured. The office, the CEO and admins only."""
    return await service.read_settings(caller)


@router.patch("/settings", response_model=MeetingSettingsRead)
async def update_meeting_settings(
    payload: MeetingSettingsUpdate,
    caller: CurrentUserDep,
    service: MeetingRequestServiceDep,
) -> MeetingSettingsRead:
    """Admin-only: name the CEO, point at their calendar, and set the rules.

    Not the Founder's Office, who would otherwise be able to appoint their own
    approver (rule 5.5).
    """
    return await service.update_settings(caller, payload)
