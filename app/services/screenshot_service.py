"""Screenshot business rules.

Upload is authenticated as a device (HMAC over the image bytes happens at the
edge in `get_current_device`); we stamp the server receive time and validate the
content type/size. Reads are scoped to the caller's visible employees — an
out-of-scope screenshot id returns 404 (never leaks existence, rule 5.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.screenshot import Screenshot
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.schemas.auth import CurrentDevice, CurrentUser
from app.services.monitoring_gate import MonitoringGateService

logger = get_logger("app.screenshot")

MAX_LIST = 60
# Full-screen captures (esp. multi-monitor Windows desktops) are larger than the
# old 5 MB cap, which was silently 422-rejecting them and stalling capture. The
# agent downscales, but keep generous headroom so a busy desktop is never dropped.
MAX_IMAGE_BYTES = 15_000_000
ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class ScreenshotService:
    def __init__(
        self,
        screenshots: ScreenshotRepository,
        employees: EmployeeRepository,
        settings: Settings,
        gate: MonitoringGateService,
    ) -> None:
        self._screenshots = screenshots
        self._employees = employees
        self._settings = settings
        self._gate = gate

    async def ingest(
        self,
        device: CurrentDevice,
        *,
        captured_at: datetime,
        content_type: str,
        width: int,
        height: int,
        image: bytes,
        monitors: list[list[int]] | None = None,
    ) -> Screenshot | None:
        # Capture only during an open work session on a working day — before any S3
        # upload. Outside that window (before check-in, after checkout, non-working
        # day like Sunday) the screenshot is dropped. See MonitoringGateService.
        if await self._gate.should_suppress(device.employee_id):
            return None

        if content_type not in ALLOWED_TYPES:
            raise ValidationError("Unsupported image type.")
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValidationError("Image missing or too large.")

        received_at = datetime.now(UTC)
        flags: list[str] = []
        if abs((received_at - captured_at).total_seconds()) > 3600:
            flags.append("clock_skew")

        # Prefer S3: upload the bytes and store only the key, keeping the DB
        # small. Without S3 configured, fall back to bytes in the `image` column.
        # If S3 is configured but the upload FAILS, also fall back to the DB so a
        # storage hiccup (bad creds, bucket, region) never silently drops every
        # screenshot — capture stays working while the S3 issue is fixed.
        object_key: str | None = None
        stored_image: bytes | None = image
        if self._settings.s3_enabled:
            key = storage.object_key(str(device.employee_id), uuid.uuid4().hex, content_type)
            try:
                await storage.put_object(key, image, content_type)
                object_key = key
                stored_image = None
            except (ClientError, BotoCoreError):
                logger.warning("screenshot_s3_put_failed_db_fallback", extra={"key": key})

        return await self._screenshots.add(
            device_id=device.device_id,
            employee_id=device.employee_id,
            captured_at=captured_at,
            content_type=content_type,
            width=max(0, width),
            height=max(0, height),
            byte_size=len(image),
            object_key=object_key,
            image=stored_image,
            monitors=_sanitize_monitors(monitors, width, height),
            flags=flags,
        )

    async def list_for_caller(self, caller: CurrentUser, limit: int) -> Sequence[Screenshot]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        return await self._screenshots.list_recent(ids, min(max(1, limit), MAX_LIST))

    async def get_image(self, caller: CurrentUser, screenshot_id: uuid.UUID) -> Screenshot:
        shot = await self._screenshots.get(screenshot_id)
        if shot is None or not await self._employees.can_read(caller, shot.employee_id):
            raise NotFoundError()
        return shot

    async def purge_old(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.screenshot_retention_days)
        # Delete the S3 blobs first so purged rows never orphan their objects.
        if self._settings.s3_enabled:
            await storage.delete_objects(await self._screenshots.object_keys_before(cutoff))
        return await self._screenshots.purge_before(cutoff)


# Agent-reported, therefore untrusted (rule 5.1): keep only well-formed rectangles
# that fit inside the claimed image, cap the count, and drop everything otherwise.
# Used by the OCR worker to crop per-monitor; a bad rect there just means we skip
# the crop, never a crash — but validate at the edge regardless.
_MAX_MONITORS = 16


def _sanitize_monitors(
    monitors: list[list[int]] | None, width: int, height: int
) -> list[list[int]]:
    if not monitors:
        return []
    clean: list[list[int]] = []
    for rect in monitors:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            continue
        try:
            x, y, w, h = (int(v) for v in rect)
        except (TypeError, ValueError):
            continue
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            continue
        # Clamp (don't drop) rects that overshoot by rounding so a monitor is never
        # silently lost; an origin fully outside the image is unusable, so skip it.
        if width > 0 and height > 0:
            if x >= width or y >= height:
                continue
            w = min(w, width - x)
            h = min(h, height - y)
        clean.append([x, y, w, h])
        if len(clean) >= _MAX_MONITORS:
            break
    return clean
