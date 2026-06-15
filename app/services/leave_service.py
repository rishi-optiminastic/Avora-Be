"""Leave business rules.

Reads are scoped in the repository. An employee applies for their own leave; only
a manager/HR/admin (never the requester) may decide it; only the requester may
withdraw their own pending request. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.models.leave import Leave, LeaveStatus
from app.models.leave_comment import LeaveComment
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import LeaveRepository
from app.repositories.leave_comment import LeaveCommentRepository
from app.schemas.auth import CurrentUser
from app.schemas.leave import LeaveCommentCreate, LeaveCreate, LeaveDecision


class LeaveService:
    def __init__(
        self,
        leaves: LeaveRepository,
        comments: LeaveCommentRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._leaves = leaves
        self._comments = comments
        self._employees = employees
        self._audit = audit

    async def list_for_caller(
        self, caller: CurrentUser, *, offset: int, limit: int, status: LeaveStatus | None = None
    ) -> tuple[Sequence[tuple[Leave, int]], int]:
        """Returns (leave, comment_count) pairs."""
        return await self._leaves.list_for_scope(caller, offset=offset, limit=limit, status=status)

    async def get_for_caller(self, caller: CurrentUser, leave_id: uuid.UUID) -> Leave:
        leave = await self._leaves.get_in_scope(caller, leave_id)
        if leave is None:
            raise NotFoundError()
        return leave

    async def apply(self, caller: CurrentUser, payload: LeaveCreate) -> Leave:
        # Employees apply for themselves; their manager is the default reviewer.
        leave = await self._leaves.create(
            payload, employee_id=caller.employee_id, reviewer_id=caller.manager_id
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.apply",
            target=f"leave:{leave.id}:{payload.leave_type.value}",
        )
        return leave

    async def decide(
        self, caller: CurrentUser, leave_id: uuid.UUID, payload: LeaveDecision
    ) -> Leave:
        leave = await self._leaves.get_in_scope(caller, leave_id)
        if leave is None:
            raise NotFoundError()
        # Only a manager/HR/admin may decide, and never your own request.
        if not caller.is_manager or caller.employee_id == leave.employee_id:
            raise AuthorizationError()
        if leave.status is not LeaveStatus.SUBMITTED:
            raise ConflictError("This request can no longer be decided.")

        leave.status = LeaveStatus.APPROVED if payload.approve else LeaveStatus.REJECTED
        leave.reviewer_id = caller.employee_id
        leave.decided_at = datetime.now(UTC)
        leave.decision_note = payload.note
        await self._leaves.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.decide",
            target=f"leave:{leave.id}:{leave.status.value}",
        )
        return leave

    async def cancel(self, caller: CurrentUser, leave_id: uuid.UUID) -> Leave:
        leave = await self._leaves.get(leave_id)
        if leave is None:
            raise NotFoundError()
        if leave.employee_id != caller.employee_id:
            raise AuthorizationError()
        if leave.status not in (LeaveStatus.SUBMITTED, LeaveStatus.DRAFT):
            raise ConflictError("Only a pending request can be withdrawn.")
        leave.status = LeaveStatus.WITHDRAWN
        await self._leaves.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.withdraw",
            target=f"leave:{leave.id}",
        )
        return leave

    async def list_comments(
        self, caller: CurrentUser, leave_id: uuid.UUID
    ) -> Sequence[LeaveComment]:
        # Only people who can see the leave may read its thread.
        if await self._leaves.get_in_scope(caller, leave_id) is None:
            raise NotFoundError()
        return await self._comments.list_for_leave(leave_id)

    async def add_comment(
        self, caller: CurrentUser, leave_id: uuid.UUID, payload: LeaveCommentCreate
    ) -> LeaveComment:
        if await self._leaves.get_in_scope(caller, leave_id) is None:
            raise NotFoundError()
        comment = await self._comments.create(
            leave_id=leave_id, author_id=caller.employee_id, body=payload.body
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.comment",
            target=f"leave:{leave_id}",
        )
        return comment
