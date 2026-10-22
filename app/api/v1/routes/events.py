"""Event endpoints - HR and admins run them; everyone else sees their own.

Authorization lives in `EventService` (rule: a route never decides who may see
or change anything), so an out-of-scope caller gets 403 on a write and 404 on a
read of something that is not theirs to know about.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, EventServiceDep
from app.schemas.event import EventRead, EventWrite

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventRead])
async def list_events(caller: CurrentUserDep, service: EventServiceDep) -> list[EventRead]:
    """Every event for HR and admins, including drafts. For everyone else, the
    published and cancelled events they are in the audience for."""
    return await service.list_for(caller)


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(
    payload: EventWrite, caller: CurrentUserDep, service: EventServiceDep
) -> EventRead:
    """Create a draft. Nobody is told until it is published."""
    return await service.create(caller, payload)


@router.get("/{event_id}", response_model=EventRead)
async def get_event(
    event_id: uuid.UUID, caller: CurrentUserDep, service: EventServiceDep
) -> EventRead:
    return await service.get(caller, event_id)


@router.put("/{event_id}", response_model=EventRead)
async def update_event(
    event_id: uuid.UUID,
    payload: EventWrite,
    caller: CurrentUserDep,
    service: EventServiceDep,
) -> EventRead:
    return await service.update(caller, event_id, payload)


@router.post("/{event_id}/publish", response_model=EventRead)
async def publish_event(
    event_id: uuid.UUID, caller: CurrentUserDep, service: EventServiceDep
) -> EventRead:
    """Make it live, tell the audience, and send its form if it has one.
    Idempotent: publishing twice does not send the form twice."""
    return await service.publish(caller, event_id)


@router.post("/{event_id}/cancel", response_model=EventRead)
async def cancel_event(
    event_id: uuid.UUID, caller: CurrentUserDep, service: EventServiceDep
) -> EventRead:
    """Call it off. The audience keeps seeing it, marked cancelled."""
    return await service.cancel(caller, event_id)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID, caller: CurrentUserDep, service: EventServiceDep
) -> None:
    """Drafts only. A published event is cancelled, not deleted."""
    await service.delete(caller, event_id)
