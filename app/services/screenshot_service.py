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

from app.core.exceptions import NotFoundError, ValidationError
from app.models.screenshot import Screenshot
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.schemas.auth import CurrentDevice, CurrentUser

MAX_LIST = 60
MAX_IMAGE_BYTES = 5_000_000
ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
RETENTION_DAYS = 14


class ScreenshotService:
    def __init__(
        self, screenshots: ScreenshotRepository, employees: EmployeeRepository
    ) -> None:
        self._screenshots = screenshots
        self._employees = employees

    async def ingest(
        self,
        device: CurrentDevice,
        *,
        captured_at: datetime,
        content_type: str,
        width: int,
        height: int,
        image: bytes,
    ) -> Screenshot:
        if content_type not in ALLOWED_TYPES:
            raise ValidationError("Unsupported image type.")
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValidationError("Image missing or too large.")

        received_at = datetime.now(UTC)
        flags: list[str] = []
        if abs((received_at - captured_at).total_seconds()) > 3600:
            flags.append("clock_skew")

        return await self._screenshots.add(
            device_id=device.device_id,
            employee_id=device.employee_id,
            captured_at=captured_at,
            content_type=content_type,
            width=max(0, width),
            height=max(0, height),
            image=image,
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
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        return await self._screenshots.purge_before(cutoff)
