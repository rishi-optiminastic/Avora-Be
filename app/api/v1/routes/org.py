"""Org / workspace endpoints — the workspace identity and white-label config that
drives the dashboard chrome. Read: any signed-in user · write: Admin only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core import storage
from app.core.deps import CurrentUserDep, OrgSettingsServiceDep, UploadRateLimitDep
from app.core.exceptions import NotFoundError
from app.core.http import read_capped_body
from app.schemas.org_settings import OrgSettingsRead, OrgSettingsUpdate
from app.services.org_settings_service import MAX_LOGO_BYTES

router = APIRouter(prefix="/org", tags=["org"])


@router.get("/settings", response_model=OrgSettingsRead)
async def get_settings(
    caller: CurrentUserDep,
    service: OrgSettingsServiceDep,
) -> OrgSettingsRead:
    return OrgSettingsRead.from_model(await service.get_or_create())


@router.put("/settings", response_model=OrgSettingsRead)
async def update_settings(
    payload: OrgSettingsUpdate,
    caller: CurrentUserDep,
    service: OrgSettingsServiceDep,
) -> OrgSettingsRead:
    return OrgSettingsRead.from_model(await service.update(caller, payload))


@router.post("/logo", response_model=OrgSettingsRead, status_code=status.HTTP_201_CREATED)
async def upload_logo(
    request: Request,
    caller: UploadRateLimitDep,
    service: OrgSettingsServiceDep,
    content_type: Annotated[str, Header()] = "application/octet-stream",
) -> OrgSettingsRead:
    """Upload the workspace logo (image bytes in the body, ≤2 MB). Admin only."""
    data = await read_capped_body(request, MAX_LOGO_BYTES)
    return OrgSettingsRead.from_model(await service.set_logo(caller, data, content_type))


@router.get("/logo")
async def get_logo(
    caller: CurrentUserDep,
    service: OrgSettingsServiceDep,
) -> Response:
    """The workspace logo, for any signed-in user (404 if none).

    S3-backed logos redirect to a short-lived presigned URL; DB-fallback logos
    stream their bytes inline (they render in an <img>)."""
    org = await service.get_logo()
    media = org.logo_content_type or "image/png"
    if org.logo_object_key:
        return RedirectResponse(storage.presigned_get_url(org.logo_object_key), 307)
    if org.logo_content is not None:
        return Response(content=org.logo_content, media_type=media)
    raise NotFoundError()
