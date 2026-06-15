"""Leave endpoints — every read is scoped to the authenticated caller."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CommentRateLimitDep, CurrentUserDep, LeaveServiceDep
from app.models.leave import LeaveStatus
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.leave import (
    LeaveCommentCreate,
    LeaveCommentRead,
    LeaveCreate,
    LeaveDecision,
    LeaveRead,
)

router = APIRouter(prefix="/leaves", tags=["leaves"])


@router.get("", response_model=Page[LeaveRead])
async def list_leaves(
    caller: CurrentUserDep,
    service: LeaveServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    leave_status: Annotated[LeaveStatus | None, Query(alias="status")] = None,
) -> Page[LeaveRead]:
    offset = (page - 1) * size
    items, total = await service.list_for_caller(
        caller, offset=offset, limit=size, status=leave_status
    )

    def _read(leave: object, count: int) -> LeaveRead:
        read = LeaveRead.model_validate(leave)
        read.comment_count = count
        return read

    return Page[LeaveRead](
        items=[_read(leave, count) for leave, count in items],
        page=page,
        size=size,
        total=total,
    )


@router.post("", response_model=LeaveRead, status_code=status.HTTP_201_CREATED)
async def apply_leave(
    payload: LeaveCreate,
    caller: CurrentUserDep,
    service: LeaveServiceDep,
) -> LeaveRead:
    return LeaveRead.model_validate(await service.apply(caller, payload))


@router.get("/{leave_id}", response_model=LeaveRead)
async def get_leave(
    leave_id: uuid.UUID,
    caller: CurrentUserDep,
    service: LeaveServiceDep,
) -> LeaveRead:
    return LeaveRead.model_validate(await service.get_for_caller(caller, leave_id))


@router.post("/{leave_id}/decision", response_model=LeaveRead)
async def decide_leave(
    leave_id: uuid.UUID,
    payload: LeaveDecision,
    caller: CurrentUserDep,
    service: LeaveServiceDep,
) -> LeaveRead:
    """Approve or reject a submitted request (reviewer / HR / admin only)."""
    return LeaveRead.model_validate(await service.decide(caller, leave_id, payload))


@router.post("/{leave_id}/withdraw", response_model=LeaveRead)
async def withdraw_leave(
    leave_id: uuid.UUID,
    caller: CurrentUserDep,
    service: LeaveServiceDep,
) -> LeaveRead:
    """The requester withdraws their own pending request."""
    return LeaveRead.model_validate(await service.cancel(caller, leave_id))


@router.get("/{leave_id}/comments", response_model=list[LeaveCommentRead])
async def list_leave_comments(
    leave_id: uuid.UUID,
    caller: CurrentUserDep,
    service: LeaveServiceDep,
) -> list[LeaveCommentRead]:
    """The discussion thread on a leave — visible to anyone who can see the leave."""
    comments = await service.list_comments(caller, leave_id)
    return [LeaveCommentRead.model_validate(c) for c in comments]


@router.post(
    "/{leave_id}/comments", response_model=LeaveCommentRead, status_code=status.HTTP_201_CREATED
)
async def add_leave_comment(
    leave_id: uuid.UUID,
    payload: LeaveCommentCreate,
    caller: CommentRateLimitDep,
    service: LeaveServiceDep,
) -> LeaveCommentRead:
    """Rate-limited per user so a copied request can't be looped to spam."""
    return LeaveCommentRead.model_validate(await service.add_comment(caller, leave_id, payload))
