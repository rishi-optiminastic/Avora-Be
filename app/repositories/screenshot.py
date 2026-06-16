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

from app.models.screenshot import Screenshot


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
        image: bytes,
        flags: list[str],
    ) -> Screenshot:
        shot = Screenshot(
            device_id=device_id,
            employee_id=employee_id,
            captured_at=captured_at,
            content_type=content_type,
            width=width,
            height=height,
            byte_size=len(image),
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

    async def purge_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(Screenshot).where(Screenshot.received_at < cutoff)
        )
        await self._session.commit()
        return cast("CursorResult[Any]", result).rowcount or 0
