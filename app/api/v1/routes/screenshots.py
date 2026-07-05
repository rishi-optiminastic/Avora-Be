"""Screenshot endpoints.

Upload is authenticated as a device — `get_current_device` verifies the HMAC
over the exact image bytes before the handler runs. Reads are scoped to the
caller's visible employees; the raw image is served one-by-one via its own
endpoint (404 when out of scope).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.core import storage
from app.core.deps import CurrentDeviceDep, CurrentUserDep, ScreenshotServiceDep
from app.core.exceptions import NotFoundError
from app.schemas.screenshot import ScreenshotRead

router = APIRouter(prefix="/screenshots", tags=["screenshots"])


def _parse_captured_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_monitors(value: str | None) -> list[list[int]]:
    """Parse the agent's `X-Monitors` header — `x,y,w,h;x,y,w,h;…` — into rects.
    Best-effort: malformed entries are skipped; the service re-validates against the
    image bounds. Absent/blank ⇒ [] (the whole image is treated as one screen)."""
    if not value:
        return []
    rects: list[list[int]] = []
    for part in value.split(";"):
        nums = part.split(",")
        if len(nums) != 4:
            continue
        try:
            rects.append([int(n) for n in nums])
        except ValueError:
            continue
    return rects


@router.post("", response_model=ScreenshotRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_screenshot(
    request: Request,
    device: CurrentDeviceDep,
    service: ScreenshotServiceDep,
    content_type: Annotated[str, Header()] = "image/jpeg",
    x_captured_at: Annotated[str | None, Header()] = None,
    x_width: Annotated[int, Header()] = 0,
    x_height: Annotated[int, Header()] = 0,
    x_monitors: Annotated[str | None, Header()] = None,
) -> ScreenshotRead | JSONResponse:
    image = await request.body()  # same bytes get_current_device already HMAC-verified
    shot = await service.ingest(
        device,
        captured_at=_parse_captured_at(x_captured_at),
        content_type=content_type,
        width=x_width,
        height=x_height,
        image=image,
        monitors=_parse_monitors(x_monitors),
    )
    if shot is None:  # outside the capture window — accepted but not stored
        return JSONResponse(
            {"accepted": False, "reason": "not_capturing"},
            status_code=status.HTTP_202_ACCEPTED,
        )
    return ScreenshotRead.model_validate(shot)


@router.get("", response_model=list[ScreenshotRead])
async def list_screenshots(
    caller: CurrentUserDep,
    service: ScreenshotServiceDep,
    limit: Annotated[int, Query(ge=1, le=60)] = 40,
) -> list[ScreenshotRead]:
    shots = await service.list_for_caller(caller, limit)
    return [ScreenshotRead.model_validate(s) for s in shots]


@router.get("/{screenshot_id}")
async def get_screenshot_image(
    screenshot_id: uuid.UUID,
    caller: CurrentUserDep,
    service: ScreenshotServiceDep,
) -> Response:
    """The image, scoped to the caller (404 if out of scope).

    For S3-backed rows we redirect to a short-lived presigned URL so the bytes
    come straight from S3 (the same-origin BFF proxy follows the redirect).
    Legacy rows still stream their in-DB bytes.
    """
    shot = await service.get_image(caller, screenshot_id)
    if shot.object_key:
        return RedirectResponse(storage.presigned_get_url(shot.object_key), status_code=307)
    if shot.image is not None:
        return Response(content=shot.image, media_type=shot.content_type)
    raise NotFoundError()
