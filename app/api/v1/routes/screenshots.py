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

from app.core.deps import CurrentDeviceDep, CurrentUserDep, ScreenshotServiceDep
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


@router.post("", response_model=ScreenshotRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_screenshot(
    request: Request,
    device: CurrentDeviceDep,
    service: ScreenshotServiceDep,
    content_type: Annotated[str, Header()] = "image/jpeg",
    x_captured_at: Annotated[str | None, Header()] = None,
    x_width: Annotated[int, Header()] = 0,
    x_height: Annotated[int, Header()] = 0,
) -> ScreenshotRead:
    image = await request.body()  # same bytes get_current_device already HMAC-verified
    shot = await service.ingest(
        device,
        captured_at=_parse_captured_at(x_captured_at),
        content_type=content_type,
        width=x_width,
        height=x_height,
        image=image,
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
    """Raw image bytes, scoped to the caller (404 if out of scope)."""
    shot = await service.get_image(caller, screenshot_id)
    return Response(content=shot.image, media_type=shot.content_type)
