"""Hidden-domain data access — list/add/remove an employee's hidden domains,
plus a batch fetch the browsing read uses to filter every employee at once."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.browsing_hidden_domain import BrowsingHiddenDomain


class BrowsingHiddenDomainRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[BrowsingHiddenDomain]:
        rows = await self._session.execute(
            select(BrowsingHiddenDomain)
            .where(BrowsingHiddenDomain.employee_id == employee_id)
            .order_by(BrowsingHiddenDomain.domain.asc())
        )
        return rows.scalars().all()

    async def domains_for_employees(
        self, employee_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, set[str]]:
        """Batch: {employee_id → {hidden domain, ...}} for the browsing filter."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(BrowsingHiddenDomain.employee_id, BrowsingHiddenDomain.domain).where(
                BrowsingHiddenDomain.employee_id.in_(employee_ids)
            )
        )
        result: dict[uuid.UUID, set[str]] = {}
        for emp_id, domain in rows.all():
            result.setdefault(emp_id, set()).add(domain)
        return result

    async def find(self, employee_id: uuid.UUID, domain: str) -> BrowsingHiddenDomain | None:
        row = await self._session.execute(
            select(BrowsingHiddenDomain).where(
                BrowsingHiddenDomain.employee_id == employee_id,
                BrowsingHiddenDomain.domain == domain,
            )
        )
        return row.scalar_one_or_none()

    async def add(self, employee_id: uuid.UUID, domain: str) -> BrowsingHiddenDomain:
        existing = await self.find(employee_id, domain)
        if existing is not None:
            return existing  # idempotent — hiding the same domain twice is a no-op
        row = BrowsingHiddenDomain(employee_id=employee_id, domain=domain)
        self._session.add(row)
        await self._session.flush()
        return row

    async def remove(self, employee_id: uuid.UUID, hidden_id: uuid.UUID) -> int:
        """Delete by id, scoped to the owner so no one removes another's row."""
        result = await self._session.execute(
            delete(BrowsingHiddenDomain).where(
                BrowsingHiddenDomain.id == hidden_id,
                BrowsingHiddenDomain.employee_id == employee_id,
            )
        )
        await self._session.flush()
        return cast("CursorResult[Any]", result).rowcount or 0
