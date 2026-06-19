"""Work-attribution endpoint — what each in-scope person is working on now."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.deps import AttributionServiceDep, CurrentUserDep
from app.schemas.attribution import AttributionRead

router = APIRouter(prefix="/attribution", tags=["attribution"])


@router.get("/now", response_model=list[AttributionRead])
async def attribution_now(
    caller: CurrentUserDep,
    service: AttributionServiceDep,
) -> list[AttributionRead]:
    return await service.live(caller, datetime.now(UTC))
