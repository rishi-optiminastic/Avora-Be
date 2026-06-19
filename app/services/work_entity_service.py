"""Work-entity (attribution catalog) business rules — admin-managed."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.employee import Role
from app.models.work_entity import WorkEntity
from app.repositories.audit import AuditRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.work_entity import WorkEntityCreate, WorkEntityUpdate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role is Role.ADMIN


class WorkEntityService:
    def __init__(self, entities: WorkEntityRepository, audit: AuditRepository) -> None:
        self._entities = entities
        self._audit = audit

    async def list_all(self, caller: CurrentUser) -> Sequence[WorkEntity]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return await self._entities.list_all()

    async def list_active_for_picker(self, caller: CurrentUser) -> Sequence[WorkEntity]:
        """Active projects a manager can attach to a task. Admin curates them;
        any manager/HR may read the active set to assign work against."""
        if not caller.is_manager:
            raise AuthorizationError()
        return await self._entities.list_active()

    async def create(self, caller: CurrentUser, payload: WorkEntityCreate) -> WorkEntity:
        if not _can_manage(caller):
            raise AuthorizationError()
        entity = await self._entities.create(payload, actor_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="work_entity.create",
            target=f"work_entity:{entity.id}",
        )
        return entity

    async def update(
        self, caller: CurrentUser, entity_id: uuid.UUID, payload: WorkEntityUpdate
    ) -> WorkEntity:
        if not _can_manage(caller):
            raise AuthorizationError()
        entity = await self._entities.get(entity_id)
        if entity is None:
            raise NotFoundError()
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(entity, key, value)
        entity.updated_by = caller.employee_id
        await self._entities.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="work_entity.update",
            target=f"work_entity:{entity.id}",
        )
        return entity

    async def delete(self, caller: CurrentUser, entity_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        entity = await self._entities.get(entity_id)
        if entity is None:
            raise NotFoundError()
        await self._entities.delete(entity)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="work_entity.delete",
            target=f"work_entity:{entity_id}",
        )
