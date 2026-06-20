"""Regularization data access — requests, approved-day lookups, monthly counts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.regularization import Regularization, RegularizationStatus


class RegularizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, employee_id: uuid.UUID, day: str, reason: str | None
    ) -> Regularization:
        reg = Regularization(employee_id=employee_id, day=day, reason=reason)
        self._session.add(reg)
        await self._session.flush()
        return reg

    async def get(self, reg_id: uuid.UUID) -> Regularization | None:
        return await self._session.get(Regularization, reg_id)

    async def get_active_for_day(self, employee_id: uuid.UUID, day: str) -> Regularization | None:
        """A pending or approved request already on that day (block duplicates)."""
        rows = await self._session.execute(
            select(Regularization).where(
                Regularization.employee_id == employee_id,
                Regularization.day == day,
                Regularization.status != RegularizationStatus.REJECTED,
            )
        )
        return rows.scalars().first()

    async def list_for_employees(
        self,
        employee_ids: Sequence[uuid.UUID],
        *,
        month: str | None = None,
        status: RegularizationStatus | None = None,
    ) -> Sequence[Regularization]:
        if not employee_ids:
            return []
        stmt = select(Regularization).where(Regularization.employee_id.in_(employee_ids))
        if month is not None:
            stmt = stmt.where(Regularization.day.startswith(month))
        if status is not None:
            stmt = stmt.where(Regularization.status == status)
        rows = await self._session.execute(stmt.order_by(Regularization.day.desc()))
        return rows.scalars().all()

    async def approved_days(
        self, employee_ids: Sequence[uuid.UUID], start_day: str, end_day: str
    ) -> dict[uuid.UUID, set[str]]:
        """Approved regularization days per employee within [start_day, end_day]."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(Regularization.employee_id, Regularization.day).where(
                Regularization.employee_id.in_(employee_ids),
                Regularization.status == RegularizationStatus.APPROVED,
                Regularization.day >= start_day,
                Regularization.day <= end_day,
            )
        )
        out: dict[uuid.UUID, set[str]] = {}
        for emp_id, day in rows.all():
            out.setdefault(emp_id, set()).add(day)
        return out

    async def count_approved_in_month(self, employee_id: uuid.UUID, month: str) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(Regularization)
            .where(
                Regularization.employee_id == employee_id,
                Regularization.status == RegularizationStatus.APPROVED,
                Regularization.day.startswith(month),
            )
        )
        return int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()
