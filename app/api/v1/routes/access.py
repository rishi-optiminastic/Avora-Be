"""Access endpoints - the tech-asset catalog, grants, the audited reveal, and
access requests. Every rule lives in the services; routes only parse and
return response schemas."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.core.deps import (
    AccessPeopleServiceDep,
    AccessRequestServiceDep,
    AccessServiceDep,
    AccessSpendServiceDep,
    CurrentUserDep,
)
from app.schemas.access import (
    AccessRequestCreate,
    AccessRequestRead,
    AccessRequestReview,
    BrandCreate,
    BrandRead,
    BrandUpdate,
    GrantCreate,
    GrantRead,
    MyGrantRead,
    PersonAccessRead,
    RevokeAllResult,
    SecretRevealRead,
    SecretWrite,
    SpendSummaryRead,
    TechAssetCreate,
    TechAssetRead,
    TechAssetUpdate,
)

router = APIRouter(prefix="/access", tags=["access"])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/me", response_model=list[MyGrantRead])
async def my_access(caller: CurrentUserDep, service: AccessServiceDep) -> list[MyGrantRead]:
    """What the caller holds right now."""
    return await service.my_grants(caller)


@router.get("/brands", response_model=list[BrandRead])
async def list_brands(caller: CurrentUserDep, service: AccessServiceDep) -> list[BrandRead]:
    """Every brand, with how many live assets carry it. Visible to everyone."""
    return await service.brands()


@router.post("/brands", response_model=BrandRead, status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandCreate, caller: CurrentUserDep, service: AccessServiceDep
) -> BrandRead:
    return await service.create_brand(caller, payload)


@router.patch("/brands/{brand_id}", response_model=BrandRead)
async def update_brand(
    brand_id: uuid.UUID, payload: BrandUpdate, caller: CurrentUserDep, service: AccessServiceDep
) -> BrandRead:
    return await service.update_brand(caller, brand_id, payload)


@router.delete("/brands/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(
    brand_id: uuid.UUID, caller: CurrentUserDep, service: AccessServiceDep
) -> None:
    await service.delete_brand(caller, brand_id)


@router.get("/assets", response_model=list[TechAssetRead])
async def list_assets(
    caller: CurrentUserDep, service: AccessServiceDep, include_archived: bool = False
) -> list[TechAssetRead]:
    """The catalog. Holders, cost and details are filtered to the caller."""
    return await service.catalog(caller, include_archived=include_archived)


@router.get("/assets/{asset_id}", response_model=TechAssetRead)
async def get_asset(
    asset_id: uuid.UUID, caller: CurrentUserDep, service: AccessServiceDep
) -> TechAssetRead:
    return await service.asset(caller, asset_id)


@router.post("/assets", response_model=TechAssetRead, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: TechAssetCreate, caller: CurrentUserDep, service: AccessServiceDep
) -> TechAssetRead:
    return await service.create(caller, payload)


@router.patch("/assets/{asset_id}", response_model=TechAssetRead)
async def update_asset(
    asset_id: uuid.UUID,
    payload: TechAssetUpdate,
    caller: CurrentUserDep,
    service: AccessServiceDep,
) -> TechAssetRead:
    return await service.update(caller, asset_id, payload)


@router.put("/assets/{asset_id}/secret", response_model=TechAssetRead)
async def rotate_secret(
    asset_id: uuid.UUID, payload: SecretWrite, caller: CurrentUserDep, service: AccessServiceDep
) -> TechAssetRead:
    return await service.rotate_secret(caller, asset_id, payload.secret)


@router.post("/assets/{asset_id}/reveal", response_model=SecretRevealRead)
async def reveal_secret(
    asset_id: uuid.UUID, caller: CurrentUserDep, service: AccessServiceDep
) -> SecretRevealRead:
    """The only way a password leaves the server. Holder or admin; audited."""
    return await service.reveal(caller, asset_id)


@router.post("/grants", response_model=GrantRead, status_code=status.HTTP_201_CREATED)
async def grant_access(
    payload: GrantCreate, caller: CurrentUserDep, service: AccessServiceDep
) -> GrantRead:
    return GrantRead.model_validate(await service.grant(caller, payload))


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_access(
    grant_id: uuid.UUID, caller: CurrentUserDep, service: AccessServiceDep
) -> None:
    await service.revoke(caller, grant_id)


@router.post("/requests", response_model=AccessRequestRead, status_code=status.HTTP_201_CREATED)
async def request_access(
    payload: AccessRequestCreate, caller: CurrentUserDep, service: AccessRequestServiceDep
) -> AccessRequestRead:
    return await service.request(caller, payload)


@router.get("/requests", response_model=list[AccessRequestRead])
async def list_requests(
    caller: CurrentUserDep, service: AccessRequestServiceDep
) -> list[AccessRequestRead]:
    """The caller's own requests, plus the pending ones they may decide."""
    return await service.list_for_caller(caller)


@router.post("/requests/{request_id}/cancel", response_model=AccessRequestRead)
async def cancel_request(
    request_id: uuid.UUID, caller: CurrentUserDep, service: AccessRequestServiceDep
) -> AccessRequestRead:
    return await service.cancel(caller, request_id)


@router.post("/requests/{request_id}/review", response_model=AccessRequestRead)
async def review_request(
    request_id: uuid.UUID,
    payload: AccessRequestReview,
    caller: CurrentUserDep,
    service: AccessRequestServiceDep,
) -> AccessRequestRead:
    return await service.review(caller, request_id, payload)


@router.get("/spend", response_model=SpendSummaryRead)
async def spend_summary(caller: CurrentUserDep, service: AccessSpendServiceDep) -> SpendSummaryRead:
    """Finance view: monthly and annual tech spend by team, category and kind."""
    return await service.summary(caller)


@router.get("/spend/export")
async def spend_export(caller: CurrentUserDep, service: AccessSpendServiceDep) -> Response:
    """The same spend as an .xlsx workbook (Summary, Line items, Renewals)."""
    data, filename = await service.export(caller)
    return Response(
        content=data,
        media_type=_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/people", response_model=list[PersonAccessRead])
async def access_by_person(
    caller: CurrentUserDep, service: AccessPeopleServiceDep
) -> list[PersonAccessRead]:
    """Everyone in the caller's scope and everything they can reach, including
    what comes through an account. An executive gets only themselves."""
    return await service.people(caller)


@router.get("/people/{employee_id}", response_model=PersonAccessRead)
async def access_for_person(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: AccessPeopleServiceDep
) -> PersonAccessRead:
    return await service.person(caller, employee_id)


@router.post("/people/{employee_id}/revoke-all", response_model=RevokeAllResult)
async def revoke_all_access(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: AccessPeopleServiceDep
) -> RevokeAllResult:
    """HR/admin offboarding: end every direct grant this person holds."""
    return await service.revoke_all(caller, employee_id)
