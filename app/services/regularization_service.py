"""Regularization business rules.

An employee requests regularization for their own late day. Their direct manager
or HR approves or rejects it (never their own) - the same rule as leave; approval
spends one of the employee's monthly credits (capped by the policy's
`monthly_regularizations`) and makes that day classify as FULL_DAY. Reads are
scoped; writes are audited; the requester is told the outcome.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
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
        for recipient_id in await self._reviewers_for(caller):
            await self._notifications.notify(
                recipient_id=recipient_id,
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

    async def _reviewers_for(self, caller: CurrentUser) -> list[uuid.UUID]:
        """Who is told about a new request.

        The reporting manager, who decides it. Without a notification the request
        was written and nobody was told - which is how seven of them sat pending.

        When there is no manager on file, or the one on file has left (inactive),
        the request falls to HR - who can decide it - and the admins, who own the
        org chart and can put the missing manager back. Otherwise it would sit
        stranded with nobody ever told.
        """
        if caller.manager_id is not None:
            manager = await self._employees.get(caller.manager_id)
            if manager is not None and manager.is_active:
                return [manager.id]
        fallback = [
            *await self._employees.list_by_role(Role.HR),
            *await self._employees.list_admins(),
        ]
        return list(dict.fromkeys(e.id for e in fallback if e.id != caller.employee_id))

    async def decidable_ids(
        self, caller: CurrentUser, regs: Sequence[Regularization]
    ) -> set[uuid.UUID]:
        """The requests in `regs` the caller may approve or reject right now - the
        same rule `review` enforces, batched for a list (one employee lookup)."""
        requesters = await self._employees.get_many(list({r.employee_id for r in regs}))
        out: set[uuid.UUID] = set()
        for reg in regs:
            requester = requesters.get(reg.employee_id)
            manager_id = requester.manager_id if requester is not None else None
            if reg.status is RegularizationStatus.PENDING and _may_decide(caller, reg, manager_id):
                out.add(reg.id)
        return out

    async def review(
        self, caller: CurrentUser, reg_id: uuid.UUID, payload: RegularizationReview
    ) -> Regularization:
        reg = await self._regs.get(reg_id)
        if reg is None or not await self._employees.can_read(caller, reg.employee_id):
            raise NotFoundError()
        if reg.employee_id == caller.employee_id:
            raise AuthorizationError()  # can't review your own
        requester = await self._employees.get(reg.employee_id)
        manager_id = requester.manager_id if requester is not None else None
        if not _may_decide(caller, reg, manager_id):
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
        await self._notify_decision(caller, reg)
        return reg

    async def _notify_decision(self, caller: CurrentUser, reg: Regularization) -> None:
        """Tell the requester how their attendance fix was decided. There is no
        dedicated notification kind for this (adding one needs a migration of the
        Postgres enum), so it goes out as SYSTEM with the attendance link."""
        approved = reg.status is RegularizationStatus.APPROVED
        await self._notifications.notify(
            recipient_id=reg.employee_id,
            kind=NotificationKind.SYSTEM,
            title=f"Attendance fix {'approved' if approved else 'rejected'}",
            body=(f"{reg.day} · {reg.review_note}" if reg.review_note else reg.day)[:200],
            level=NotificationLevel.INFO if approved else NotificationLevel.WARNING,
            link=_ATTENDANCE_LINK,
            entity_type="regularization",
            entity_id=reg.id,
            actor_id=caller.employee_id,
        )


def _may_decide(
    caller: CurrentUser, reg: Regularization, requester_manager_id: uuid.UUID | None
) -> bool:
    """Who may approve or reject an attendance fix: the requester's direct
    manager, or HR. Never your own.

    The same rule as leave (`leave_rules.may_decide`). Being a manager is not the
    permission - being THEIR manager is: the old gate was `caller.is_manager`, a
    role check, so a team lead holding the EMPLOYEE role got a 403 on their own
    reports while any MANAGER-titled person could act on anyone they could see.

    Admin (and senior manager) is deliberately NOT a decider by role. Nothing in
    the product relies on it - the review queue only ever offered every visible
    request - and attendance fixes are the line manager's call. An admin who is
    somebody's manager still decides for that person. Admins are still told about
    managerless requests (see `_reviewers_for`) because fixing the reporting line
    is theirs to do.
    """
    if caller.employee_id == reg.employee_id:
        return False
    if caller.role is Role.HR:
        return True
    return requester_manager_id is not None and requester_manager_id == caller.employee_id
