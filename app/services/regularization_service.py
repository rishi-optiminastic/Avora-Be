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
from app.models.notification import NotificationKind
from app.models.regularization import Regularization, RegularizationStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.regularization import RegularizationRepository
from app.schemas.auth import CurrentUser
from app.schemas.regularization import RegularizationCreate, RegularizationReview
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.notification_service import NotificationService

_ATTENDANCE_LINK = "/dashboard/time/attendance"


class RegularizationService:
    def __init__(
        self,
        regularizations: RegularizationRepository,
        employees: EmployeeRepository,
        policy: AttendancePolicyService,
        audit: AuditRepository,
        notifications: NotificationService,
    ) -> None:
        self._regs = regularizations
        self._employees = employees
        self._policy = policy
        self._audit = audit
        self._notifications = notifications

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
        # Tell the reporting manager. Without this the request was written and
        # nobody was told: the manager could see and approve it all along, but
        # only if they happened to open the page — which is how seven of them
        # sat pending.
        if caller.manager_id is not None:
            await self._notifications.notify(
                recipient_id=caller.manager_id,
                kind=NotificationKind.REGULARIZATION_REQUEST,
                title="Attendance fix to review",
                body=f"{payload.day} · {payload.reason}"[:200],
                link=_ATTENDANCE_LINK,
                entity_type="regularization",
                entity_id=reg.id,
                actor_id=caller.employee_id,
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

    async def _may_decide(self, caller: CurrentUser, reg: Regularization) -> bool:
        """Who may approve or reject this request.

        Being a manager is not the permission - being THEIR manager is. The gate
        used to be `caller.is_manager` alone, a ROLE check, so someone who runs a
        team while holding the EMPLOYEE or EXECUTIVE role could see their reports'
        requests and got a 403 on every attempt to action one. Five of Rashi
        Chheda's sat pending for exactly that reason, with nobody able to clear
        them. Leave approval already decides this by relationship
        (`LeaveService._may_decide`); this is the same rule.

        The role check stays as an OR, not a replacement: HR and senior managers
        action requests outside their own direct line today, and narrowing to a
        pure relationship test would take that away.
        """
        if caller.is_manager:
            return True
        requester = await self._employees.get(reg.employee_id)
        return requester is not None and requester.manager_id == caller.employee_id

    async def review(
        self, caller: CurrentUser, reg_id: uuid.UUID, payload: RegularizationReview
    ) -> Regularization:
        reg = await self._regs.get(reg_id)
        if reg is None or not await self._employees.can_read(caller, reg.employee_id):
            raise NotFoundError()
        if reg.employee_id == caller.employee_id:
            raise AuthorizationError()  # can't review your own
        if not await self._may_decide(caller, reg):
            raise AuthorizationError()
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
