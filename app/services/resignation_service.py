"""Resignation business rules.

An employee submits their own resignation; only HR/Admin may decide it (never
their own — segregation of duties). Reads are scoped in the repository (own +
HR/Admin all). Accepting does NOT deactivate the account — offboarding stays a
separate deliberate HR action. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.resignation import Resignation, ResignationStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.resignation import ResignationRepository
from app.schemas.auth import CurrentUser
from app.schemas.resignation import ResignationCreate, ResignationDecision
from app.services.email_service import EmailError, EmailService
from app.services.notification_service import NotificationService

logger = get_logger("app.resignation")

_LINK = "/dashboard/time/resignations"


def _can_review(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class ResignationService:
    def __init__(
        self,
        resignations: ResignationRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        email: EmailService,
    ) -> None:
        self._resignations = resignations
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._email = email

    async def list_for_caller(
        self, caller: CurrentUser, *, offset: int, limit: int
    ) -> tuple[Sequence[Resignation], int]:
        return await self._resignations.list_for_scope(caller, offset=offset, limit=limit)

    async def get_for_caller(self, caller: CurrentUser, resignation_id: uuid.UUID) -> Resignation:
        row = await self._resignations.get_in_scope(caller, resignation_id)
        if row is None:
            raise NotFoundError()
        return row

    async def submit(self, caller: CurrentUser, payload: ResignationCreate) -> Resignation:
        if await self._resignations.has_active(caller.employee_id):
            raise ConflictError("You already have a resignation under review.")
        row = await self._resignations.create(payload, employee_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="resignation.submit",
            target=f"resignation:{row.id}",
        )
        resigner = await self._employees.get(caller.employee_id)
        resigner_name = resigner.full_name if resigner is not None else "An employee"
        body = f"{resigner_name} · last day {payload.last_working_day:%d %b %Y}"
        # Notify + email every HR and Admin — they decide it.
        reviewers = [
            *await self._employees.list_by_role(Role.ADMIN),
            *await self._employees.list_by_role(Role.HR),
        ]
        seen: set[uuid.UUID] = set()
        for reviewer in reviewers:
            if reviewer.id in seen or reviewer.id == caller.employee_id:
                continue
            seen.add(reviewer.id)
            await self._notifications.notify(
                recipient_id=reviewer.id,
                kind=NotificationKind.RESIGNATION_SUBMITTED,
                title="Resignation to review",
                body=body,
                level=NotificationLevel.WARNING,
                link=_LINK,
                entity_type="resignation",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )
            await self._email_submitted(reviewer, resigner_name, row)
        return row

    async def _email_submitted(
        self, reviewer: Employee, resigner_name: str, row: Resignation
    ) -> None:
        try:
            await self._email.send_resignation_submitted(
                to=reviewer.work_email,
                recipient_name=reviewer.full_name,
                resigner_name=resigner_name,
                last_working_label=f"{row.last_working_day:%d %b %Y}",
                reason=row.reason,
                link_path=_LINK,
            )
        except EmailError:
            logger.warning("resignation_submitted_email_failed", extra={"id": str(row.id)})

    async def decide(
        self, caller: CurrentUser, resignation_id: uuid.UUID, payload: ResignationDecision
    ) -> Resignation:
        row = await self._resignations.get(resignation_id)
        if row is None:
            raise NotFoundError()
        # HR/Admin only, and never your own resignation.
        if not _can_review(caller) or caller.employee_id == row.employee_id:
            raise AuthorizationError()
        if row.status is not ResignationStatus.SUBMITTED:
            raise ConflictError("This resignation can no longer be decided.")

        accepted = payload.accept
        row.status = ResignationStatus.ACCEPTED if accepted else ResignationStatus.REJECTED
        row.reviewer_id = caller.employee_id
        row.decided_at = datetime.now(UTC)
        row.decision_note = payload.note
        await self._resignations.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="resignation.decide",
            target=f"resignation:{row.id}:{row.status.value}",
        )
        await self._notifications.notify(
            recipient_id=row.employee_id,
            kind=NotificationKind.RESIGNATION_DECISION,
            title=f"Resignation {'accepted' if accepted else 'declined'}",
            body=payload.note,
            level=NotificationLevel.INFO,
            link=_LINK,
            entity_type="resignation",
            entity_id=row.id,
            actor_id=caller.employee_id,
        )
        await self._email_decision(row, caller, accepted=accepted, note=payload.note)
        return row

    async def _email_decision(
        self, row: Resignation, caller: CurrentUser, *, accepted: bool, note: str | None
    ) -> None:
        recipient = await self._employees.get(row.employee_id)
        decider = await self._employees.get(caller.employee_id)
        if recipient is None:
            return
        decided_by = decider.full_name if decider is not None else "HR"
        try:
            await self._email.send_resignation_decision(
                to=recipient.work_email,
                employee_name=recipient.full_name,
                accepted=accepted,
                last_working_label=f"{row.last_working_day:%d %b %Y}",
                decided_by=decided_by,
                note=note,
                link_path=_LINK,
            )
        except EmailError:
            logger.warning("resignation_decision_email_failed", extra={"id": str(row.id)})

    async def withdraw(self, caller: CurrentUser, resignation_id: uuid.UUID) -> Resignation:
        row = await self._resignations.get(resignation_id)
        if row is None:
            raise NotFoundError()
        if row.employee_id != caller.employee_id:
            raise AuthorizationError()
        if row.status is not ResignationStatus.SUBMITTED:
            raise ConflictError("Only a pending resignation can be withdrawn.")
        row.status = ResignationStatus.WITHDRAWN
        await self._resignations.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="resignation.withdraw",
            target=f"resignation:{row.id}",
        )
        return row
