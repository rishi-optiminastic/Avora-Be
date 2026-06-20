"""Regularization business rules.

An employee requests regularization for their own late day. A manager/HR who can
see them approves or rejects (never their own); approval spends one of the
employee's monthly credits (capped by the policy's `monthly_regularizations`) and
makes that day classify as FULL_DAY. Reads are scoped; writes are audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.regularization import Regularization, RegularizationStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.regularization import RegularizationRepository
from app.schemas.auth import CurrentUser
from app.schemas.regularization import RegularizationCreate, RegularizationReview
from app.services.attendance_policy_service import AttendancePolicyService


class RegularizationService:
    def __init__(
        self,
        regularizations: RegularizationRepository,
        employees: EmployeeRepository,
        policy: AttendancePolicyService,
        audit: AuditRepository,
    ) -> None:
        self._regs = regularizations
        self._employees = employees
        self._policy = policy
        self._audit = audit

    async def request(self, caller: CurrentUser, payload: RegularizationCreate) -> Regularization:
        existing = await self._regs.get_active_for_day(caller.employee_id, payload.day)
        if existing is not None:
            raise ValidationError("A regularization for that day already exists.")
        reg = await self._regs.create(
            employee_id=caller.employee_id, day=payload.day, reason=payload.reason
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="regularization.request",
            target=f"regularization:{reg.id}:{payload.day}",
        )
        return reg

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        month: str | None = None,
        status: RegularizationStatus | None = None,
    ) -> Sequence[Regularization]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        return await self._regs.list_for_employees(ids, month=month, status=status)

    async def review(
        self, caller: CurrentUser, reg_id: uuid.UUID, payload: RegularizationReview
    ) -> Regularization:
        reg = await self._regs.get(reg_id)
        if reg is None or not await self._employees.can_read(caller, reg.employee_id):
            raise NotFoundError()
        if not caller.is_manager:
            raise AuthorizationError()
        if reg.employee_id == caller.employee_id:
            raise AuthorizationError()  # can't review your own
        if reg.status is not RegularizationStatus.PENDING:
            raise ValidationError("This regularization was already reviewed.")

        if payload.approve:
            limit = (await self._policy.spec()).monthly_regularizations
            used = await self._regs.count_approved_in_month(reg.employee_id, reg.day[:7])
            if used >= limit:
                raise ValidationError("No regularization credits left this month.")
            reg.status = RegularizationStatus.APPROVED
        else:
            reg.status = RegularizationStatus.REJECTED
        reg.reviewed_by_id = caller.employee_id
        reg.review_note = payload.review_note
        await self._regs.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action=f"regularization.{reg.status.value}",
            target=f"regularization:{reg.id}",
        )
        return reg
