"""Out-of-office business rules.

Someone claims a day was worked away from the office; their direct manager - or
HR - approves it, and the day then counts as worked without a punch behind it.

Three rules carry the weight:

* **The manager decides, never the requester.** Same rule as leave and
  regularization (`_may_decide`). Approving your own would be writing yourself a
  paid day.
* **No credit cap.** A regularization spends one of two monthly credits because
  it corrects a mistake. Being sent to the warehouse is not a mistake, and
  rationing it would push real field work back into absence.
* **HR is always told.** This pays a day that no biometric punch supports, so the
  people who run payroll hear about every approval.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.out_of_office import OutOfOffice, OutOfOfficePortion, OutOfOfficeStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import LeaveRepository
from app.repositories.out_of_office import OutOfOfficeRepository
from app.schemas.auth import CurrentUser
from app.schemas.out_of_office import OutOfOfficeCreate, OutOfOfficeReview
from app.services.email_service import EmailService
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

_LINK = "/dashboard/time/out-of-office"
OFFICE_TZ = "Asia/Kolkata"

_PORTION_LABEL = {
    OutOfOfficePortion.FULL_DAY: "Full day",
    OutOfOfficePortion.HALF_DAY: "Half day",
}


def _day_label(entry: OutOfOffice) -> str:
    one_day = entry.start_day == entry.end_day
    span = entry.start_day if one_day else f"{entry.start_day} to {entry.end_day}"
    return f"{span} ({_PORTION_LABEL[entry.portion].lower()})"


class OutOfOfficeService:
    def __init__(
        self,
        entries: OutOfOfficeRepository,
        employees: EmployeeRepository,
        leaves: LeaveRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        email: EmailService,
    ) -> None:
        self._entries = entries
        self._employees = employees
        self._leaves = leaves
        self._audit = audit
        self._notifications = notifications
        self._email = email

    async def _assert_not_on_leave(
        self, employee_id: uuid.UUID, start_day: str, end_day: str
    ) -> None:
        """Refuse days already covered by approved paid leave.

        Payroll counts worked days from attendance and paid-leave days from the
        leave table, independently. A day that is both would be credited twice:
        `min(elapsed_working, present + paid)` stops anyone being overpaid, but
        the spare day silently forgives a genuine absence elsewhere in the month.
        Cheaper to refuse the overlap than to reconcile it later.
        """
        tz = ZoneInfo(OFFICE_TZ)
        start = datetime.fromisoformat(f"{start_day}T00:00:00").replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{end_day}T23:59:59").replace(tzinfo=tz)
        clashes = await self._leaves.approved_paid_in_range(
            [employee_id], start.astimezone(UTC), end.astimezone(UTC)
        )
        if clashes.get(employee_id):
            raise ValidationError(
                "You have approved leave on those dates. Withdraw the leave first, "
                "or pick days you are not already on leave."
            )

    async def request(self, caller: CurrentUser, payload: OutOfOfficeCreate) -> OutOfOffice:
        start_day, end_day = payload.start_day.isoformat(), payload.end_day.isoformat()
        clash = await self._entries.overlapping(caller.employee_id, start_day, end_day)
        if clash is not None:
            raise ValidationError(
                f"You already have an out-of-office entry covering "
                f"{clash.start_day} to {clash.end_day}."
            )
        await self._assert_not_on_leave(caller.employee_id, start_day, end_day)
        entry = await self._entries.create(
            employee_id=caller.employee_id,
            start_day=start_day,
            end_day=end_day,
            portion=payload.portion,
            location=payload.location.strip(),
            reason=payload.reason.strip(),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="out_of_office.request",
            target=f"out_of_office:{entry.id}:{start_day}..{end_day}",
        )
        await self._tell_reviewers(caller, entry)
        return entry

    async def _tell_reviewers(self, caller: CurrentUser, entry: OutOfOffice) -> None:
        """Notify whoever can decide this. A request nobody is told about just
        sits there - the same way seven regularizations once did."""
        for reviewer in await self._reviewers_for(caller):
            await self._notifications.notify(
                recipient_id=reviewer.id,
                kind=NotificationKind.SYSTEM,
                title="Out-of-office request to review",
                body=f"{_day_label(entry)} · {entry.location}"[:200],
                link=_LINK,
                entity_type="out_of_office",
                entity_id=entry.id,
                actor_id=caller.employee_id,
            )

    async def _reviewers_for(self, caller: CurrentUser) -> list[Employee]:
        """The reporting manager, or HR plus the admins when there isn't one.

        An employee with no manager on file - or one who has left - would
        otherwise have nobody to ask, and the request would never be decided.
        """
        if caller.manager_id is not None:
            manager = await self._employees.get(caller.manager_id)
            if manager is not None and manager.is_active:
                return [manager]
        fallback = [
            *await self._employees.list_by_role(Role.HR),
            *await self._employees.list_admins(),
        ]
        seen: dict[uuid.UUID, Employee] = {e.id: e for e in fallback if e.id != caller.employee_id}
        return list(seen.values())

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        month: str | None = None,
        status: OutOfOfficeStatus | None = None,
    ) -> Sequence[OutOfOffice]:
        employees = await self._employees.all_in_scope(caller)
        return await self._entries.list_for_employees(
            [e.id for e in employees], month=month, status=status
        )

    async def decidable_ids(
        self, caller: CurrentUser, entries: Sequence[OutOfOffice]
    ) -> set[uuid.UUID]:
        """Which of these the caller may decide right now - the same rule `review`
        enforces, batched so a list costs one employee lookup."""
        requesters = await self._employees.get_many(list({e.employee_id for e in entries}))
        out: set[uuid.UUID] = set()
        for entry in entries:
            requester = requesters.get(entry.employee_id)
            manager_id = requester.manager_id if requester is not None else None
            if entry.status is OutOfOfficeStatus.PENDING and _may_decide(caller, entry, manager_id):
                out.add(entry.id)
        return out

    async def cancel(self, caller: CurrentUser, entry_id: uuid.UUID) -> OutOfOffice:
        """Withdraw your own request before anyone has decided it."""
        entry = await self._entries.get(entry_id)
        if entry is None or entry.employee_id != caller.employee_id:
            raise NotFoundError()
        if entry.status is not OutOfOfficeStatus.PENDING:
            raise ValidationError("That request was already reviewed.")
        entry.status = OutOfOfficeStatus.CANCELLED
        await self._entries.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="out_of_office.cancelled",
            target=f"out_of_office:{entry.id}",
        )
        return entry

    async def review(
        self, caller: CurrentUser, entry_id: uuid.UUID, payload: OutOfOfficeReview
    ) -> OutOfOffice:
        entry = await self._entries.get(entry_id)
        if entry is None or not await self._employees.can_read(caller, entry.employee_id):
            raise NotFoundError()
        if entry.employee_id == caller.employee_id:
            raise AuthorizationError()  # approving your own is writing yourself a day
        requester = await self._employees.get(entry.employee_id)
        manager_id = requester.manager_id if requester is not None else None
        if not _may_decide(caller, entry, manager_id):
            raise AuthorizationError()
        if entry.status is not OutOfOfficeStatus.PENDING:
            raise ValidationError("That request was already reviewed.")

        entry.status = OutOfOfficeStatus.APPROVED if payload.approve else OutOfOfficeStatus.REJECTED
        entry.reviewed_by_id = caller.employee_id
        entry.review_note = payload.review_note
        entry.decided_at = datetime.now(UTC)
        await self._entries.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action=f"out_of_office.{entry.status.value}",
            target=f"out_of_office:{entry.id}",
        )
        await self._notify_decision(caller, entry, requester)
        return entry

    async def _notify_decision(
        self, caller: CurrentUser, entry: OutOfOffice, requester: Employee | None
    ) -> None:
        """Tell the requester, and put HR on the mail when it was approved.

        An approval pays a day with no punch behind it, so the people who run
        payroll should see it without having to go looking. Email failure never
        fails the decision - the record is already written.
        """
        approved = entry.status is OutOfOfficeStatus.APPROVED
        await self._notifications.notify(
            recipient_id=entry.employee_id,
            kind=NotificationKind.SYSTEM,
            title=f"Out of office {'approved' if approved else 'rejected'}",
            body=f"{_day_label(entry)} · {entry.location}"[:200],
            level=NotificationLevel.INFO if approved else NotificationLevel.WARNING,
            link=_LINK,
            entity_type="out_of_office",
            entity_id=entry.id,
            actor_id=caller.employee_id,
        )
        if not approved or requester is None or not requester.work_email:
            return
        approver = await self._employees.get(caller.employee_id)
        try:
            await self._email.send_out_of_office_approved(
                to=requester.work_email,
                requester_name=requester.full_name,
                approver_name=approver.full_name if approver else "Your manager",
                day_label=_day_label(entry),
                location=entry.location,
                reason=entry.reason,
                link_path=_LINK,
            )
        except Exception:  # delivery must never undo a decision already written
            logger.warning("out_of_office_email_failed", extra={"entry_id": str(entry.id)})


def _may_decide(
    caller: CurrentUser, entry: OutOfOffice, requester_manager_id: uuid.UUID | None
) -> bool:
    """Who may approve or reject: the requester's direct manager, or HR. Never
    your own.

    The same rule as leave and regularization. Being *a* manager is not the
    permission - being THEIR manager is - so a team lead carrying the executive
    role still decides for their own reports, and a manager elsewhere in the org
    cannot sign off a day they know nothing about.
    """
    if caller.employee_id == entry.employee_id:
        return False
    if caller.role is Role.HR:
        return True
    return requester_manager_id is not None and requester_manager_id == caller.employee_id
