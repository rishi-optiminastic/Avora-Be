"""Reimbursement business rules — a two-step expense-claim approval.

An employee submits their own claim. Their reporting MANAGER approves or rejects
it first (never their own; HR/Admin may also act at this step when there is no
manager). Only a manager-approved claim reaches HR/Admin for FINAL approval.
Approved claims are read by payroll (org-wide) via the repository.

Reads are scoped in the repository. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.reimbursement import Reimbursement, ReimbursementStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.reimbursement import ReimbursementRepository
from app.schemas.auth import CurrentUser
from app.schemas.reimbursement import ReimbursementCreate, ReimbursementDecision
from app.services.notification_service import NotificationService

_LINK = "/dashboard/time/reimbursements"


def _can_review_hr(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


def _money(amount_minor: int) -> str:
    return f"₹{amount_minor / 100:,.2f}"


class ReimbursementService:
    def __init__(
        self,
        reimbursements: ReimbursementRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
    ) -> None:
        self._reimbursements = reimbursements
        self._employees = employees
        self._audit = audit
        self._notifications = notifications

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: ReimbursementStatus | None = None,
    ) -> tuple[Sequence[Reimbursement], int]:
        return await self._reimbursements.list_for_scope(
            caller, offset=offset, limit=limit, status=status
        )

    async def get_for_caller(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement:
        row = await self._reimbursements.get_in_scope(caller, reimbursement_id)
        if row is None:
            raise NotFoundError()
        return row

    async def submit(self, caller: CurrentUser, payload: ReimbursementCreate) -> Reimbursement:
        period = ReimbursementRepository.period_for(payload.expense_date)
        row = await self._reimbursements.create(
            payload, employee_id=caller.employee_id, period_month=period
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.submit",
            target=f"reimbursement:{row.id}",
        )
        await self._notify_step1_reviewers(caller, row)
        return row

    async def _notify_step1_reviewers(
        self, caller: CurrentUser, row: Reimbursement
    ) -> None:
        """Ping whoever performs the manager step: the reporting manager, or every
        HR/Admin when the employee has no manager on file."""
        applicant = await self._employees.get(caller.employee_id)
        who = applicant.full_name if applicant is not None else "An employee"
        body = f"{who} · {_money(row.amount_minor)} · {row.category.value}"
        recipients: list[uuid.UUID] = []
        if caller.manager_id is not None:
            recipients.append(caller.manager_id)
        else:
            recipients = [
                r.id
                for r in (
                    *await self._employees.list_by_role(Role.ADMIN),
                    *await self._employees.list_by_role(Role.HR),
                )
            ]
        seen: set[uuid.UUID] = set()
        for recipient_id in recipients:
            if recipient_id in seen or recipient_id == caller.employee_id:
                continue
            seen.add(recipient_id)
            await self._notifications.notify(
                recipient_id=recipient_id,
                kind=NotificationKind.REIMBURSEMENT_SUBMITTED,
                title="Reimbursement to review",
                body=body,
                level=NotificationLevel.INFO,
                link=_LINK,
                entity_type="reimbursement",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )

    async def manager_decide(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, payload: ReimbursementDecision
    ) -> Reimbursement:
        """Step 1: the applicant's reporting manager (or HR/Admin) approves/rejects."""
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if caller.employee_id == row.employee_id:
            raise AuthorizationError()  # never your own claim
        if not await self._is_manager_reviewer(caller, row):
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.SUBMITTED:
            raise ConflictError("This claim is no longer awaiting manager review.")

        row.manager_reviewer_id = caller.employee_id
        row.manager_decided_at = datetime.now(UTC)
        row.manager_note = payload.note
        row.status = (
            ReimbursementStatus.MANAGER_APPROVED
            if payload.approve
            else ReimbursementStatus.REJECTED
        )
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.manager_decide",
            target=f"reimbursement:{row.id}:{row.status.value}",
        )
        if payload.approve:
            await self._notify_hr_reviewers(caller, row)
        else:
            await self._notify_applicant(row, approved=False, note=payload.note)
        return row

    async def hr_decide(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, payload: ReimbursementDecision
    ) -> Reimbursement:
        """Step 2: HR/Admin final approval of a manager-approved claim."""
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if not _can_review_hr(caller) or caller.employee_id == row.employee_id:
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.MANAGER_APPROVED:
            raise ConflictError("This claim is not awaiting HR approval.")

        row.hr_reviewer_id = caller.employee_id
        row.hr_decided_at = datetime.now(UTC)
        row.hr_note = payload.note
        row.status = (
            ReimbursementStatus.APPROVED if payload.approve else ReimbursementStatus.REJECTED
        )
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.hr_decide",
            target=f"reimbursement:{row.id}:{row.status.value}",
        )
        await self._notify_applicant(row, approved=payload.approve, note=payload.note)
        return row

    async def withdraw(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement:
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if row.employee_id != caller.employee_id:
            raise AuthorizationError()
        if row.status not in (
            ReimbursementStatus.SUBMITTED,
            ReimbursementStatus.MANAGER_APPROVED,
        ):
            raise ConflictError("Only a claim still under review can be withdrawn.")
        row.status = ReimbursementStatus.WITHDRAWN
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.withdraw",
            target=f"reimbursement:{row.id}",
        )
        return row

    async def _is_manager_reviewer(
        self, caller: CurrentUser, row: Reimbursement
    ) -> bool:
        """The manager step is for the applicant's reporting manager. HR/Admin may
        also act here (e.g. when the applicant has no manager on file)."""
        if _can_review_hr(caller):
            return True
        applicant = await self._employees.get(row.employee_id)
        return applicant is not None and applicant.manager_id == caller.employee_id

    async def _notify_hr_reviewers(self, caller: CurrentUser, row: Reimbursement) -> None:
        applicant = await self._employees.get(row.employee_id)
        who = applicant.full_name if applicant is not None else "An employee"
        body = f"{who} · {_money(row.amount_minor)} · manager-approved"
        seen: set[uuid.UUID] = set()
        for reviewer in (
            *await self._employees.list_by_role(Role.ADMIN),
            *await self._employees.list_by_role(Role.HR),
        ):
            if reviewer.id in seen or reviewer.id == row.employee_id:
                continue
            seen.add(reviewer.id)
            await self._notifications.notify(
                recipient_id=reviewer.id,
                kind=NotificationKind.REIMBURSEMENT_SUBMITTED,
                title="Reimbursement for final approval",
                body=body,
                level=NotificationLevel.INFO,
                link=_LINK,
                entity_type="reimbursement",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )

    async def _notify_applicant(
        self, row: Reimbursement, *, approved: bool, note: str | None
    ) -> None:
        await self._notifications.notify(
            recipient_id=row.employee_id,
            kind=NotificationKind.REIMBURSEMENT_DECISION,
            title=f"Reimbursement {'approved' if approved else 'declined'}",
            body=note or f"{_money(row.amount_minor)} · {row.category.value}",
            level=NotificationLevel.INFO,
            link=_LINK,
            entity_type="reimbursement",
            entity_id=row.id,
            actor_id=row.hr_reviewer_id or row.manager_reviewer_id,
        )
