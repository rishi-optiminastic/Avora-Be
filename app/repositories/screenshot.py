"""Screenshot data access. List queries defer the (large) image column so the
metadata list never drags image bytes over the wire; the image is loaded only
by `get` for the single-image endpoint."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.screenshot import OcrStatus, Screenshot


class ScreenshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        device_id: uuid.UUID,
        employee_id: uuid.UUID,
        captured_at: datetime,
        content_type: str,
        width: int,
        height: int,
        byte_size: int,
        object_key: str | None,
        image: bytes | None,
        flags: list[str],
    ) -> Screenshot:
        shot = Screenshot(
            device_id=device_id,
            employee_id=employee_id,
            captured_at=captured_at,
            content_type=content_type,
            width=width,
            height=height,
            byte_size=byte_size,
            object_key=object_key,
            image=image,
            flags=flags,
        )
        self._session.add(shot)
        await self._session.flush()
        return shot

    async def list_recent(
        self, employee_ids: Sequence[uuid.UUID], limit: int
    ) -> Sequence[Screenshot]:
        if not employee_ids:
            return []
        rows = await self._session.execute(
            select(Screenshot)
            .options(defer(Screenshot.image))
            .where(Screenshot.employee_id.in_(employee_ids))
            .order_by(Screenshot.received_at.desc())
            .limit(limit)
        )
        return rows.scalars().all()

    async def get(self, screenshot_id: uuid.UUID) -> Screenshot | None:
        return await self._session.get(Screenshot, screenshot_id)

    async def latest_ocr_text(
        self, employee_ids: Sequence[uuid.UUID], since: datetime
    ) -> dict[uuid.UUID, str]:
        """Most-recent OCR'd text per employee since `since` (for attribution)."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(Screenshot.employee_id, Screenshot.ocr_text)
            .where(
                Screenshot.employee_id.in_(employee_ids),
                Screenshot.received_at >= since,
                Screenshot.ocr_status == OcrStatus.DONE,
                Screenshot.ocr_text.is_not(None),
            )
            .order_by(Screenshot.received_at.desc())
        )
        latest: dict[uuid.UUID, str] = {}
        for emp_id, text in rows.all():
            if text:
                latest.setdefault(emp_id, text)
        return latest

    async def ocr_text_for_day(
        self, employee_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[str]:
        """OCR'd text from one employee's screenshots in [start, end), oldest-first.

        Only the extracted *text* is selected — the `image` column is never loaded,
        so screenshot bytes never leave the DB for the EOD summary."""
        rows = await self._session.execute(
            select(Screenshot.ocr_text)
            .where(
                Screenshot.employee_id == employee_id,
                Screenshot.received_at >= start,
                Screenshot.received_at < end,
                Screenshot.ocr_status == OcrStatus.DONE,
                Screenshot.ocr_text.is_not(None),
            )
            .order_by(Screenshot.received_at.asc())
        )
        return [t for t in rows.scalars().all() if t]

    async def object_keys_before(self, cutoff: datetime) -> list[str]:
        """S3 keys of rows older than `cutoff` (so retention can delete the
        blobs before the rows go)."""
        rows = await self._session.execute(
            select(Screenshot.object_key).where(
                Screenshot.received_at < cutoff,
                Screenshot.object_key.is_not(None),
            )
        )
        return [k for k in rows.scalars().all() if k]

    async def purge_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(Screenshot).where(Screenshot.received_at < cutoff)
        )
        await self._session.commit()
        return cast("CursorResult[Any]", result).rowcount or 0
