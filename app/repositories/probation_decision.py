"""Probation decisions. Queries only — scope lives in the service."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.probation_decision import ProbationDecision


class ProbationDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, employee_id: uuid.UUID) -> ProbationDecision | None:
        row: ProbationDecision | None = await self._session.scalar(
            select(ProbationDecision).where(ProbationDecision.employee_id == employee_id)
        )
        return row

    async def for_employees(
        self, employee_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ProbationDecision]:
        """One query for the roster — a per-row lookup would be an N+1."""
        if not employee_ids:
            return {}
        rows = await self._session.scalars(
            select(ProbationDecision).where(ProbationDecision.employee_id.in_(employee_ids))
        )
        return {row.employee_id: row for row in rows}

    def add(self, decision: ProbationDecision) -> None:
        self._session.add(decision)

    async def flush(self) -> None:
        await self._session.flush()
