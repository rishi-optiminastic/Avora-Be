"""Work-attribution endpoints — live "working on" + manual label corrections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import (
    AttributionCorrectionServiceDep,
    AttributionServiceDep,
    CurrentUserDep,
)
from app.models.attribution_correction import CorrectionStatus
from app.schemas.attribution import AttributionRead
from app.schemas.attribution_correction import (
    CorrectionCreate,
    CorrectionRead,
    CorrectionReview,
)

router = APIRouter(prefix="/attribution", tags=["attribution"])


@router.get("/now", response_model=list[AttributionRead])
async def attribution_now(
    caller: CurrentUserDep,
    service: AttributionServiceDep,
) -> list[AttributionRead]:
    return await service.live(caller, datetime.now(UTC))


@router.post("/corrections", response_model=CorrectionRead, status_code=status.HTTP_201_CREATED)
async def propose_correction(
    payload: CorrectionCreate,
    caller: CurrentUserDep,
    service: AttributionCorrectionServiceDep,
) -> CorrectionRead:
    """An employee re-labels their own work to a project (pending manager review)."""
    return CorrectionRead.model_validate(await service.propose(caller, payload))


@router.get("/corrections", response_model=list[CorrectionRead])
async def list_corrections(
    caller: CurrentUserDep,
    service: AttributionCorrectionServiceDep,
    correction_status: Annotated[CorrectionStatus | None, Query(alias="status")] = None,
) -> list[CorrectionRead]:
    """Corrections in the caller's scope (own; a manager sees their reports')."""
    rows = await service.list_for_caller(caller, status=correction_status)
    return [CorrectionRead.model_validate(c) for c in rows]


@router.post("/corrections/{correction_id}/review", response_model=CorrectionRead)
async def review_correction(
    correction_id: uuid.UUID,
    payload: CorrectionReview,
    caller: CurrentUserDep,
    service: AttributionCorrectionServiceDep,
) -> CorrectionRead:
    """A manager approves or rejects a proposed correction (scoped, not own)."""
    return CorrectionRead.model_validate(await service.review(caller, correction_id, payload))
