"""Leave-policy data access — a single org-wide row (singleton)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave_policy import LeavePolicy


class LeavePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> LeavePolicy | None:
        rows = await self._session.execute(
            select(LeavePolicy).order_by(LeavePolicy.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self) -> LeavePolicy:
        policy = LeavePolicy()
        self._session.add(policy)
        await self._session.flush()
        return policy

    async def flush(self) -> None:
        await self._session.flush()
