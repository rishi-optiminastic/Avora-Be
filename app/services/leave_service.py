"""Leave business rules.

Reads are scoped in the repository so the requester's manager, HR and admin can
all *see* a request. But **only an admin may approve/reject** it (never the
requester) — approval is a segregation-of-duties action reserved to admins; a
manager or HR can view a request but not decide it. Only the requester may
withdraw their own pending request. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.payroll import working_days_between
from app.models.employee import Role
from app.models.leave import Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.leave_comment import LeaveCommentRepository
from app.schemas.auth import CurrentUser
from app.schemas.leave import (
    LeaveBalanceRead,
    LeaveCommentCreate,
    LeaveCreate,
    LeaveDecision,
    LeaveTypeBalance,
)
from app.services.email_service import EmailError, EmailService
from app.services.leave_policy_service import LeavePolicyService
from app.services.notification_service import NotificationService

# A half-day leave consumes half a working day; full-day types consume one.
_HALF_DAY_WEIGHT = 0.5
# Half-day time off draws from the planned-leave quota (it is planned time off).
_PLANNED_TYPES = (LeaveType.PLANNED, LeaveType.HALF_DAY)

_LEAVES_LINK = "/dashboard/time/leaves"

logger = get_logger("app.leave")


class LeaveService:
    def __init__(
        self,
        leaves: LeaveRepository,
        comments: LeaveCommentRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        email: EmailService,
        policy: LeavePolicyService,
        holidays: HolidayRepository,
    ) -> None:
        self._leaves = leaves
        self._comments = comments
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._email = email
        self._policy = policy
        self._holidays = holidays

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
        # Approval is admin-only → tell every admin there's a request to approve.
        # The reporting manager can see it too (repo scope), so notify them as an
        # FYI — unless they're already an admin (avoid a duplicate).
        body = (
            f"{payload.leave_type.value.replace('_', ' ').title()} · "
            f"{payload.start_date:%d %b} - {payload.end_date:%d %b}"
        )
        admins = await self._employees.list_by_role(Role.ADMIN)
        admin_ids = {admin.id for admin in admins}
        for admin in admins:
            await self._notifications.notify(
                recipient_id=admin.id,
                kind=NotificationKind.LEAVE_REQUEST,
                title="Leave request to approve",
                body=body,
                link=_LEAVES_LINK,
                entity_type="leave",
                entity_id=leave.id,
                actor_id=caller.employee_id,
            )
        if caller.manager_id is not None and caller.manager_id not in admin_ids:
            await self._notifications.notify(
                recipient_id=caller.manager_id,
                kind=NotificationKind.LEAVE_REQUEST,
                title="Leave request from your team",
                body=body,
                link=_LEAVES_LINK,
                entity_type="leave",
                entity_id=leave.id,
                actor_id=caller.employee_id,
            )
        return leave

    async def decide(
        self, caller: CurrentUser, leave_id: uuid.UUID, payload: LeaveDecision
    ) -> Leave:
        leave = await self._leaves.get_in_scope(caller, leave_id)
        if leave is None:
            raise NotFoundError()
        # Approval is admin-only, and never your own request. Managers/HR can see
        # the request (repo scope) but cannot decide it.
        if not caller.is_admin or caller.employee_id == leave.employee_id:
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
        approved = leave.status is LeaveStatus.APPROVED
        await self._notifications.notify(
            recipient_id=leave.employee_id,
            kind=NotificationKind.LEAVE_DECISION,
            title=f"Leave {'approved' if approved else 'rejected'}",
            body=payload.note,
            level=NotificationLevel.INFO if approved else NotificationLevel.WARNING,
            link=_LEAVES_LINK,
            entity_type="leave",
            entity_id=leave.id,
            actor_id=caller.employee_id,
        )
        await self._email_decision(leave, caller, approved=approved, note=payload.note)
        return leave

    async def _email_decision(
        self, leave: Leave, caller: CurrentUser, *, approved: bool, note: str | None
    ) -> None:
        """Email the requester their decision. Best-effort: a delivery failure
        must never roll back the decision, so we swallow and log it."""
        recipient = await self._employees.get(leave.employee_id)
        decider = await self._employees.get(caller.employee_id)
        if recipient is None:
            return
        decided_by = decider.full_name if decider is not None else "Your manager"
        leave_type_label = leave.leave_type.value.replace("_", " ").title()
        date_range = f"{leave.start_date:%d %b} - {leave.end_date:%d %b}"
        try:
            await self._email.send_leave_decision(
                to=recipient.work_email,
                employee_name=recipient.full_name,
                approved=approved,
                leave_type_label=leave_type_label,
                date_range_label=date_range,
                decided_by=decided_by,
                note=note,
                link_path=_LEAVES_LINK,
            )
        except EmailError:
            logger.warning("leave_decision_email_failed", extra={"leave_id": str(leave.id)})

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

    async def balance(
        self, caller: CurrentUser, employee_id: uuid.UUID | None = None
    ) -> LeaveBalanceRead:
        """The caller's (or an authorised viewer's) leave balances for the current
        joining-anniversary year. Days are counted as working days (weekends and
        holidays excluded), so the picture matches how leave is actually spent."""
        target_id = employee_id or caller.employee_id
        if target_id != caller.employee_id and not await self._employees.can_read(
            caller, target_id
        ):
            # Prefer 404 over 403 — revealing existence would leak scope (§7).
            raise NotFoundError()
        employee = await self._employees.get(target_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()

        anchor = employee.hire_date or employee.created_at.date()
        year_start, year_end = _leave_year_window(anchor, datetime.now(UTC).date())
        holidays = await self._holidays.dates_in_range(year_start, year_end)
        start_dt = datetime(year_start.year, year_start.month, year_start.day, tzinfo=UTC)
        end_dt = datetime(year_end.year, year_end.month, year_end.day, tzinfo=UTC) + timedelta(
            days=1
        )

        approved = await self._leaves.for_employee_in_window(
            target_id, start_dt, end_dt, [LeaveStatus.APPROVED]
        )
        pending = await self._leaves.for_employee_in_window(
            target_id, start_dt, end_dt, [LeaveStatus.SUBMITTED]
        )
        policy = await self._policy.get_or_create()

        def bucket(
            rows: list[tuple[datetime, datetime, LeaveType, LeaveStatus]],
            types: tuple[LeaveType, ...],
        ) -> float:
            total = 0.0
            for s, e, kind, _status in rows:
                if kind not in types:
                    continue
                ls = max(year_start, _utc_date(s))
                le = min(year_end, _utc_date(e))
                days = float(working_days_between(ls, le, holidays))
                total += days * (_HALF_DAY_WEIGHT if kind is LeaveType.HALF_DAY else 1.0)
            return total

        balances: list[LeaveTypeBalance] = []
        for leave_type, allocated, types in (
            (LeaveType.PLANNED, float(policy.annual_planned_days), _PLANNED_TYPES),
            (LeaveType.SICK, float(policy.annual_sick_days), (LeaveType.SICK,)),
            (LeaveType.UNPAID, 0.0, (LeaveType.UNPAID,)),
        ):
            used = bucket(approved, types)
            pend = bucket(pending, types)
            balances.append(
                LeaveTypeBalance(
                    leave_type=leave_type,
                    allocated=allocated,
                    used=used,
                    pending=pend,
                    remaining=allocated - used - pend,
                )
            )

        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.balance.read",
            target=f"employee:{target_id}",
        )
        return LeaveBalanceRead(
            leave_year_start=year_start, leave_year_end=year_end, balances=balances
        )

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


def _utc_date(value: datetime) -> date:
    """The UTC calendar date of a stored leave bound. Leaves are persisted at UTC
    midnight; a naive value (e.g. from SQLite) is treated as already-UTC rather
    than shifted by the machine's local timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def _leave_year_window(anchor: date, today: date) -> tuple[date, date]:
    """The joining-anniversary leave year containing `today`.

    Returns [start, end] inclusive, where start is the most recent anniversary of
    `anchor` on or before `today`, and end is the day before the next one. A
    Feb-29 anchor is clamped to Feb-28 in non-leap years.
    """

    def anniversary(year: int) -> date:
        try:
            return anchor.replace(year=year)
        except ValueError:  # Feb 29 in a non-leap year
            return anchor.replace(year=year, day=28)

    start = anniversary(today.year)
    if start > today:
        start = anniversary(today.year - 1)
    end = anniversary(start.year + 1) - timedelta(days=1)
    return start, end
