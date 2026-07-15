"""Assignment-grant data access. Who may grant (admin/HR) is enforced by the
service; this layer only builds queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment_grant import AssignmentGrant
from app.models.employee import Employee


class AssignmentGrantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, assigner_id: uuid.UUID, assignee_id: uuid.UUID) -> bool:
        row = await self._session.get(AssignmentGrant, (assigner_id, assignee_id))
        return row is not None

    async def assignee_ids_for(self, assigner_id: uuid.UUID) -> set[uuid.UUID]:
        """The extra people this employee may assign work to (grants only — the
        caller's own reporting scope is layered on top by the service)."""
        rows = await self._session.execute(
            select(AssignmentGrant.assignee_id).where(AssignmentGrant.assigner_id == assigner_id)
        )
        return set(rows.scalars().all())

    async def assignees_for(self, assigner_id: uuid.UUID) -> Sequence[Employee]:
        """The granted people as active employee rows, name-ordered."""
        rows = await self._session.execute(
            select(Employee)
            .join(AssignmentGrant, AssignmentGrant.assignee_id == Employee.id)
            .where(AssignmentGrant.assigner_id == assigner_id, Employee.is_active.is_(True))
            .order_by(Employee.full_name)
        )
        return rows.scalars().all()

    async def add(
        self,
        *,
        assigner_id: uuid.UUID,
        assignee_id: uuid.UUID,
        granted_by_id: uuid.UUID | None,
    ) -> None:
        """Idempotent — re-granting an existing pair is a no-op."""
        if await self.exists(assigner_id, assignee_id):
            return
        self._session.add(
            AssignmentGrant(
                assigner_id=assigner_id,
                assignee_id=assignee_id,
                granted_by_id=granted_by_id,
            )
        )
        await self._session.flush()

    async def remove(self, assigner_id: uuid.UUID, assignee_id: uuid.UUID) -> None:
        row = await self._session.get(AssignmentGrant, (assigner_id, assignee_id))
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
