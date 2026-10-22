"""What Avora mirrors from Circle about one person - read-only here.

Personal and employment details (masked ID numbers only), appraisal history and
assets. HR edits all of it in Circle; the Circle sync copies it in. Authorization
lives in `CircleRecordService`: the person, HR/Admin or payroll; anyone else 403.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import CircleRecordServiceDep, CurrentUserDep
from app.schemas.circle import AppraisalRead, AssetRead, CircleProfileRead

router = APIRouter(prefix="/employees", tags=["circle"])


@router.get("/{employee_id}/circle-profile", response_model=CircleProfileRead)
async def get_circle_profile(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CircleRecordServiceDep,
) -> CircleProfileRead:
    return await service.profile(caller, employee_id)


@router.get("/{employee_id}/appraisals", response_model=list[AppraisalRead])
async def list_appraisals(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CircleRecordServiceDep,
) -> list[AppraisalRead]:
    """Newest first; capped server-side (MAX_RECORDS_PER_PERSON)."""
    rows = await service.appraisals(caller, employee_id)
    return [AppraisalRead.model_validate(row) for row in rows]


@router.get("/{employee_id}/assets", response_model=list[AssetRead])
async def list_assets(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: CircleRecordServiceDep,
) -> list[AssetRead]:
    """Capped server-side (MAX_RECORDS_PER_PERSON)."""
    rows = await service.assets(caller, employee_id)
    return [AssetRead.model_validate(row) for row in rows]
