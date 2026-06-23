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

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.employee import TrackingMode
from app.models.screenshot import Screenshot
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.schemas.auth import CurrentDevice, CurrentUser

MAX_LIST = 60
MAX_IMAGE_BYTES = 5_000_000
ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class ScreenshotService:
    def __init__(
        self,
        screenshots: ScreenshotRepository,
        employees: EmployeeRepository,
        settings: Settings,
    ) -> None:
        self._screenshots = screenshots
        self._employees = employees
        self._settings = settings

    async def ingest(
        self,
        device: CurrentDevice,
        *,
        captured_at: datetime,
        content_type: str,
        width: int,
        height: int,
        image: bytes,
    ) -> Screenshot | None:
        # Capture gate: drop screenshots while the employee is in PERSONAL mode
        # (nothing uploaded to S3, nothing stored). None signals "paused".
        if await self._employees.tracking_mode(device.employee_id) is TrackingMode.PERSONAL:
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
        object_key: str | None = None
        stored_image: bytes | None = image
        if self._settings.s3_enabled:
            object_key = storage.object_key(str(device.employee_id), uuid.uuid4().hex, content_type)
            await storage.put_object(object_key, image, content_type)
            stored_image = None

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
