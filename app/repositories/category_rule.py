"""Category-rule data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.categories import CategoryRuleSpec
from app.models.category_rule import CategoryRule
from app.schemas.category_rule import CategoryRuleCreate


class CategoryRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: CategoryRuleCreate, *, actor_id: uuid.UUID) -> CategoryRule:
        rule = CategoryRule(
            match_type=payload.match_type,
            pattern=payload.pattern.strip(),
            category=payload.category,
            department=payload.department,
            created_by=actor_id,
            updated_by=actor_id,
        )
        self._session.add(rule)
        await self._session.flush()
        return rule

    async def get(self, rule_id: uuid.UUID) -> CategoryRule | None:
        return await self._session.get(CategoryRule, rule_id)

    async def list_all(self) -> Sequence[CategoryRule]:
        rows = await self._session.execute(
            select(CategoryRule).order_by(CategoryRule.department.is_(None), CategoryRule.pattern)
        )
        return rows.scalars().all()

    async def specs(self) -> list[CategoryRuleSpec]:
        """All rules as plain specs for the resolver (no ORM in the hot path)."""
        rows = await self._session.execute(select(CategoryRule))
        return [
            CategoryRuleSpec(
                match_type=r.match_type,
                pattern=r.pattern,
                category=r.category,
                department=r.department,
            )
            for r in rows.scalars().all()
        ]

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, rule: CategoryRule) -> None:
        await self._session.delete(rule)
        await self._session.flush()
