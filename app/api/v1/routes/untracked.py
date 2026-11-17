"""The untracked-work detector's preview. Read-only, HR and admins only.

One endpoint, deliberately. There is no "run it now" here: the detector runs
unattended on its own schedule behind a flag, and a route that wrote tasks on
request would be a second way to create work with different authorization
from the first.

Its own module rather than a route on `/tasks`, because it answers a question
about the DETECTOR - what would it do, and is the catalog good enough - not
about anybody's tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.deps import CurrentUserDep, UntrackedWorkServiceDep
from app.schemas.untracked import FoundRead, PreviewRead

router = APIRouter(prefix="/untracked", tags=["untracked"])


@router.get("/preview", response_model=PreviewRead)
async def preview(caller: CurrentUserDep, service: UntrackedWorkServiceDep) -> PreviewRead:
    """What the detector would raise right now, writing nothing.

    The intended use is the sign-off before switching the sweep on: look at
    the names, check they are right, then set the flag. `enabled` is included
    so a reviewer can tell a dry run from a live one at a glance.
    """
    outcome = await service.preview_for(caller, datetime.now(UTC))
    return PreviewRead(
        enabled=get_settings().untracked_detection_enabled,
        people_considered=outcome.considered,
        would_create=outcome.created,
        held_back=outcome.held_back,
        notes=list(outcome.notes),
        found=[
            FoundRead(
                employee_id=f.employee_id,
                employee_name=f.employee_name,
                entity_id=f.entity_id,
                entity_name=f.entity_name,
                sightings=f.sightings,
                confidence=f.confidence,
                title=f.title,
            )
            for f in outcome.found
        ],
    )
