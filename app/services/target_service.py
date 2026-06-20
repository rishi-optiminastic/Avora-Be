"""Target business rules.

Only admins and senior managers ASSIGN targets (the spec: "Managers should
receive quarterly and monthly targets from Admins or Senior Managers"). The
owner (a manager) reports progress — current value, status, review notes — on
their own targets, but can't re-scope or reassign them. Reads are scoped in the
repository; every write is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.employee import Role
from app.models.target import Target, TargetPeriod, TargetStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.target import TargetRepository
from app.schemas.auth import CurrentUser
from app.schemas.target import TargetCreate, TargetUpdate

# The owner reports progress on their own target — nothing structural.
_OWNER_EDITABLE = frozenset({"current_value", "status", "review_notes"})


def _can_assign(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.SENIOR_MANAGER)


class TargetService:
    def __init__(
        self,
        targets: TargetRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._targets = targets
        self._employees = employees
        self._audit = audit

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        period: TargetPeriod | None = None,
        status: TargetStatus | None = None,
    ) -> tuple[Sequence[Target], int]:
        return await self._targets.list_for_scope(
            caller, offset=offset, limit=limit, period=period, status=status
        )

    async def get_for_caller(self, caller: CurrentUser, target_id: uuid.UUID) -> Target:
        target = await self._targets.get_in_scope(caller, target_id)
        if target is None:
            raise NotFoundError()
        return target

    async def _validate_parent(self, caller: CurrentUser, parent_id: uuid.UUID | None) -> None:
        if parent_id is None:
            return
        parent = await self._targets.get_in_scope(caller, parent_id)
        if parent is None:
            raise ValidationError("Unknown parent target.")

    async def create(self, caller: CurrentUser, payload: TargetCreate) -> Target:
        if not _can_assign(caller):
            raise AuthorizationError()
        # You may only assign to a manager you can see.
        if not await self._employees.can_read(caller, payload.owner_id):
            raise AuthorizationError()
        await self._validate_parent(caller, payload.parent_target_id)
        target = await self._targets.create(payload, assigned_by_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="target.create",
            target=f"target:{target.id}:owner:{payload.owner_id}",
        )
        return target

    async def update(
        self, caller: CurrentUser, target_id: uuid.UUID, payload: TargetUpdate
    ) -> Target:
        target = await self._targets.get_in_scope(caller, target_id)
        if target is None:
            raise NotFoundError()

        fields = payload.model_dump(exclude_unset=True)
        if not _can_assign(caller):
            # Only the owner, and only progress fields.
            if target.owner_id != caller.employee_id or not set(fields).issubset(_OWNER_EDITABLE):
                raise AuthorizationError()
        else:
            if payload.owner_id is not None and not await self._employees.can_read(
                caller, payload.owner_id
            ):
                raise AuthorizationError()

        if "member_ids" in fields:
            fields["member_ids"] = [str(m) for m in fields["member_ids"]]
        for key, value in fields.items():
            setattr(target, key, value)

        await self._targets.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="target.update",
            target=f"target:{target.id}",
        )
        return target

    async def delete(self, caller: CurrentUser, target_id: uuid.UUID) -> None:
        target = await self._targets.get(target_id)
        if target is None:
            raise NotFoundError()
        if not caller.is_admin and target.assigned_by_id != caller.employee_id:
            raise AuthorizationError()
        await self._targets.delete(target)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="target.delete",
            target=f"target:{target_id}",
        )
