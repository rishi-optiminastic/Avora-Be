"""Notification (inbox) endpoints — every row is scoped to the caller."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, NotificationServiceDep
from app.models.notification import NotificationKind
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.notification import NotificationRead, UnreadCount

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationRead])
async def list_notifications(
    caller: CurrentUserDep,
    service: NotificationServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    unread_only: Annotated[bool, Query()] = False,
    # Narrow the inbox to one kind — lets a surface that only cares about, say,
    # escalations read them exactly instead of paging the whole inbox.
    kind: Annotated[NotificationKind | None, Query()] = None,
) -> Page[NotificationRead]:
    offset = (page - 1) * size
    items, total = await service.list_for_caller(
        caller, offset=offset, limit=size, unread_only=unread_only, kind=kind
    )
    return Page[NotificationRead](
        items=[NotificationRead.model_validate(n) for n in items],
        page=page,
        size=size,
        total=total,
    )


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(
    caller: CurrentUserDep,
    service: NotificationServiceDep,
) -> UnreadCount:
    return UnreadCount(count=await service.unread_count(caller))


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    caller: CurrentUserDep,
    service: NotificationServiceDep,
) -> None:
    await service.mark_all_read(caller)


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    notification_id: uuid.UUID,
    caller: CurrentUserDep,
    service: NotificationServiceDep,
) -> None:
    # Idempotent: marking an already-read or out-of-scope row is a no-op 204.
    await service.mark_read(caller, notification_id)
