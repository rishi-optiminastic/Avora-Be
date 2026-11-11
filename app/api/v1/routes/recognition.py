"""Recognition endpoints — award a badge, read the org feed, and the threads.

The feed is deliberately org-wide (that is the programme), so these reads are not
scoped to the caller's reporting tree the way attendance is. Writing is scoped:
`RecognitionService._may_award` holds the rule.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, RecognitionServiceDep
from app.core.recognition_badges import BADGES
from app.models.recognition import RecognitionBadge
from app.schemas.recognition import (
    BadgeRead,
    RecognitionCommentCreate,
    RecognitionCommentRead,
    RecognitionCreate,
    RecognitionRead,
)

router = APIRouter(prefix="/recognition", tags=["recognition"])


@router.get("/badges", response_model=list[BadgeRead])
async def list_badges(caller: CurrentUserDep) -> list[BadgeRead]:
    """The published badge list, so the form and the feed share one source."""
    return [
        BadgeRead(
            badge=spec.badge,
            emoji=spec.emoji,
            label=spec.label,
            cadence=spec.cadence,
            when_to_use=spec.when_to_use,
            points=spec.points,
        )
        for spec in BADGES
    ]


@router.post("", response_model=RecognitionRead, status_code=status.HTTP_201_CREATED)
async def award(
    payload: RecognitionCreate,
    caller: CurrentUserDep,
    service: RecognitionServiceDep,
) -> RecognitionRead:
    """Award a badge to one of your reports (HR/admin: to anyone)."""
    row = await service.award(caller, payload)
    feed = await service.feed(caller, employee_id=row.employee_id, limit=1)
    return feed[0]


@router.get("", response_model=list[RecognitionRead])
async def feed(
    caller: CurrentUserDep,
    service: RecognitionServiceDep,
    employee_id: uuid.UUID | None = None,
    badge: RecognitionBadge | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RecognitionRead]:
    """The recognition feed, newest first, each with its comment thread."""
    return await service.feed(
        caller, employee_id=employee_id, badge=badge, limit=limit, offset=offset
    )


@router.post(
    "/{recognition_id}/comments",
    response_model=RecognitionCommentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    recognition_id: uuid.UUID,
    payload: RecognitionCommentCreate,
    caller: CurrentUserDep,
    service: RecognitionServiceDep,
) -> RecognitionCommentRead:
    """Congratulate or add to a recognition (anyone)."""
    comment = await service.comment(caller, recognition_id, payload)
    return RecognitionCommentRead.model_validate(comment).model_copy(update={"can_delete": True})


@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: uuid.UUID,
    caller: CurrentUserDep,
    service: RecognitionServiceDep,
) -> None:
    """Remove your own comment (HR/admin may moderate any)."""
    await service.delete_comment(caller, comment_id)


@router.delete("/{recognition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unaward(
    recognition_id: uuid.UUID,
    caller: CurrentUserDep,
    service: RecognitionServiceDep,
) -> None:
    """Take back a recognition you gave (HR/admin may remove any)."""
    await service.unaward(caller, recognition_id)
