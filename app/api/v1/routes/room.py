"""The Room - the shared board on the dashboard.

Open to every authenticated employee, by design: this is the one surface in
Avora that is addressed to the whole company rather than scoped to a reporting
line. Authorization still applies to the two things that are not "say
something" - deleting someone else's note, and pinning - both of which are
checked in the service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUserDep, RoomServiceDep
from app.schemas.room import (
    RoomEventRead,
    RoomMessageCreate,
    RoomMessageRead,
    RoomRead,
    RsvpSet,
)
from app.services.room_stream import stream_messages

router = APIRouter(prefix="/room", tags=["room"])


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("", response_model=RoomRead)
async def board(caller: CurrentUserDep, service: RoomServiceDep) -> Any:
    """Today's celebrations, the next event, and the notes on both."""
    return await service.board(caller, _now())


@router.get("/messages", response_model=list[RoomMessageRead])
async def messages_since(
    caller: CurrentUserDep,
    service: RoomServiceDep,
    since: Annotated[datetime | None, Query(description="Newest note you already have")] = None,
) -> Any:
    """The polling read. Pass `since` and only later notes come back."""
    return await service.since(caller, _now(), since)


@router.get("/stream")
async def stream(caller: CurrentUserDep, service: RoomServiceDep) -> StreamingResponse:
    """Live delivery: one long-lived connection, each new note pushed as it lands.

    The topics are resolved once, here, from the server's own view of today -
    so a stream cannot be opened against someone else's board by passing keys.

    `X-Accel-Buffering: no` matters more than it looks: behind nginx (and
    Coolify's proxy) an `text/event-stream` response is buffered by default,
    which turns a live chat into one that delivers everything at once when the
    connection finally closes. The client falls back to polling if this stream
    never produces its `ready` frame.
    """
    board = await service.board(caller, _now())
    return StreamingResponse(
        stream_messages(caller, board.open_topics, _now()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/messages", response_model=RoomMessageRead, status_code=status.HTTP_201_CREATED)
async def post_message(
    payload: RoomMessageCreate, caller: CurrentUserDep, service: RoomServiceDep
) -> Any:
    """Leave a note. Anyone may; the topic must be one the Room is showing."""
    return await service.post(caller, payload, _now())


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: uuid.UUID, caller: CurrentUserDep, service: RoomServiceDep
) -> None:
    """Your own note, or anyone's if you are HR/admin."""
    await service.delete(caller, message_id)


@router.post("/messages/{message_id}/pin", response_model=RoomMessageRead)
async def pin_message(
    message_id: uuid.UUID, caller: CurrentUserDep, service: RoomServiceDep
) -> Any:
    """Pin a note to the top. HR and admin only - curation, not posting."""
    return await service.set_pin(caller, message_id, pinned=True, now=_now())


@router.delete("/messages/{message_id}/pin", response_model=RoomMessageRead)
async def unpin_message(
    message_id: uuid.UUID, caller: CurrentUserDep, service: RoomServiceDep
) -> Any:
    return await service.set_pin(caller, message_id, pinned=False, now=_now())


@router.post("/events/{event_id}/rsvp", response_model=RoomEventRead)
async def rsvp(
    event_id: uuid.UUID, payload: RsvpSet, caller: CurrentUserDep, service: RoomServiceDep
) -> Any:
    """Count me in (or out). Idempotent - answering twice updates one row."""
    return await service.rsvp(caller, event_id, attending=payload.attending)
