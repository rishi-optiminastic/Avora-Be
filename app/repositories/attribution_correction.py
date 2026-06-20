"""Attribution-correction data access — scoped lists + status updates."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribution_correction import AttributionCorrection, CorrectionStatus
from app.schemas.attribution_correction import CorrectionCreate


class AttributionCorrectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, payload: CorrectionCreate, *, employee_id: uuid.UUID
    ) -> AttributionCorrection:
        correction = AttributionCorrection(
            employee_id=employee_id,
            proposed_by_id=employee_id,
            entity_id=payload.entity_id,
            screenshot_id=payload.screenshot_id,
            for_date=payload.for_date,
            note=payload.note,
        )
        self._session.add(correction)
        await self._session.flush()
        return correction

    async def get(self, correction_id: uuid.UUID) -> AttributionCorrection | None:
        return await self._session.get(AttributionCorrection, correction_id)

    async def list_for_employees(
        self,
        employee_ids: Sequence[uuid.UUID],
        *,
        status: CorrectionStatus | None = None,
    ) -> Sequence[AttributionCorrection]:
        if not employee_ids:
            return []
        clause: ColumnElement[bool] = AttributionCorrection.employee_id.in_(employee_ids)
        stmt = select(AttributionCorrection).where(clause)
        if status is not None:
            stmt = stmt.where(AttributionCorrection.status == status)
        rows = await self._session.execute(stmt.order_by(AttributionCorrection.created_at.desc()))
        return rows.scalars().all()

    async def flush(self) -> None:
        await self._session.flush()
