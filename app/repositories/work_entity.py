"""Work-entity data access — the admin-curated attribution catalog."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work_entity import WorkEntity
from app.schemas.work_entity import WorkEntityCreate


class WorkEntityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: WorkEntityCreate, *, actor_id: uuid.UUID) -> WorkEntity:
        entity = WorkEntity(
            name=payload.name,
            department=payload.department,
            description=payload.description,
            keywords=payload.keywords,
            domains=payload.domains,
            created_by=actor_id,
            updated_by=actor_id,
        )
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def get(self, entity_id: uuid.UUID) -> WorkEntity | None:
        return await self._session.get(WorkEntity, entity_id)

    async def list_all(self) -> Sequence[WorkEntity]:
        rows = await self._session.execute(select(WorkEntity).order_by(WorkEntity.name.asc()))
        return rows.scalars().all()

    async def list_active(self) -> Sequence[WorkEntity]:
        """Active entities only — used by the attribution matcher."""
        rows = await self._session.execute(
            select(WorkEntity).where(WorkEntity.is_active.is_(True)).order_by(WorkEntity.name.asc())
        )
        return rows.scalars().all()

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, entity: WorkEntity) -> None:
        await self._session.delete(entity)
        await self._session.flush()
