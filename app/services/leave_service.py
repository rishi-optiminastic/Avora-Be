"""Leave business rules.

Reads are scoped in the repository so the requester, their manager and HR can
*see* a request. Deciding it belongs to the requester's direct manager or HR
(never the requester, and not admin as such - see `leave_rules.may_decide`).
Only the requester may withdraw their own pending request; an APPROVED leave can
be cancelled by the employee before it starts, or by HR / their manager at any
time (`leave_rules.may_cancel_approved`).

Applying (and approving) also enforces the leave rules the UI warns about:
tenure eligibility, the available balance and no double-booked days.
No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.payroll import working_days_between
from app.core.tenure import TenureStatus, accrued_units, probation_end, tenure_status
from app.models.employee import Employee, Gender, Role
from app.models.leave import HalfDayPeriod, Leave, LeaveStatus, LeaveType
from app.models.leave_comment import LeaveComment
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.leave import LeaveRepository
from app.repositories.leave_allocation import LeaveAllocationRepository
from app.repositories.leave_comment import LeaveCommentRepository
from app.repositories.leave_tier_quota import LeaveTierQuotaRepository
from app.schemas.auth import CurrentUser
from app.schemas.leave import (
    LeaveBalanceRead,
    LeaveCancel,
    LeaveCommentCreate,
    LeaveCreate,
    LeaveDecision,
    LeaveTypeBalance,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.email_service import EmailError, EmailService
from app.services.leave_policy_service import LeavePolicyService
from app.services.leave_rules import (
    can_decide_now,
    clashes,
    days_in_window,
    leave_weight,
    may_cancel_approved,
    may_decide,
    utc_date,
)
from app.services.notification_service import NotificationService

# Half-day time off draws from the planned-leave quota (it is planned time off).
_PLANNED_TYPES = (LeaveType.PLANNED, LeaveType.HALF_DAY)
# Paid half days a person on probation may book per calendar month. Planned
# leave is locked then, so this is the only limit on them.
PROBATION_HALF_DAYS_PER_MONTH = 2

# Types that must be applied in advance (planned time off). Event-based leave
# (sick, bereavement, birthday, maternity, paternity, marriage) and unpaid can be
# applied any time (still never for a past date — see _enforce_not_past).
_NOTICE_TYPES = (LeaveType.PLANNED, LeaveType.ANNUAL)

# The paid, quota-tracked leave types shown on the balance. Each row is:
#   (leave type, LeavePolicy attr, LeaveAllocation override attr, member types)
# "member types" are the leave types whose approved/pending days count against
# this quota (half-day rolls up into planned). UNPAID is uncapped and handled
# separately. Adding a new tracked type is a single row here + the two columns.
_TRACKED_LEAVE: tuple[tuple[LeaveType, str, str, tuple[LeaveType, ...]], ...] = (
    (LeaveType.PLANNED, "annual_planned_days", "planned_days", _PLANNED_TYPES),
    (LeaveType.ANNUAL, "annual_days", "annual_days", (LeaveType.ANNUAL,)),
    (LeaveType.SICK, "annual_sick_days", "sick_days", (LeaveType.SICK,)),
    (LeaveType.BEREAVEMENT, "bereavement_days", "bereavement_days", (LeaveType.BEREAVEMENT,)),
    (LeaveType.BIRTHDAY, "birthday_days", "birthday_days", (LeaveType.BIRTHDAY,)),
    (LeaveType.MATERNITY, "maternity_days", "maternity_days", (LeaveType.MATERNITY,)),
    (LeaveType.PATERNITY, "paternity_days", "paternity_days", (LeaveType.PATERNITY,)),
    (LeaveType.MARRIAGE, "marriage_days", "marriage_days", (LeaveType.MARRIAGE,)),
)

_LEAVES_LINK = "/dashboard/time/leaves"


def _quota_bucket(leave_type: LeaveType) -> LeaveType | None:
    """The tracked quota a leave type spends from (half-day -> planned), or None
    for an uncapped type such as unpaid leave."""
    for bucket, _policy_attr, _alloc_attr, members in _TRACKED_LEAVE:
        if leave_type in members:
            return bucket
    return None


@dataclass(frozen=True)
class _Request:
    """The part of a leave the apply/approve rules look at, from either a new
    request or a stored one."""

    leave_type: LeaveType
    start: datetime
    end: datetime
    half_day: HalfDayPeriod | None

    @classmethod
    def from_create(cls, payload: LeaveCreate) -> _Request:
        return cls(
            payload.leave_type, payload.start_date, payload.end_date, payload.half_day_period
        )

    @classmethod
    def from_leave(cls, leave: Leave) -> _Request:
        return cls(leave.leave_type, leave.start_date, leave.end_date, leave.half_day_period)


@dataclass(frozen=True)
class LeavePermissions:
    """What one caller may do with one leave right now."""

    can_decide: bool
    can_cancel: bool


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
        allocations: LeaveAllocationRepository,
        attendance_policy: AttendancePolicyService,
        tiers: LeaveTierQuotaRepository,
    ) -> None:
        self._leaves = leaves
        self._comments = comments
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._email = email
        self._policy = policy
        self._holidays = holidays
        self._allocations = allocations
        self._attendance_policy = attendance_policy
        self._tiers = tiers

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

    async def _org_today(self) -> date:
        """Today's date in the org's policy timezone (never the server's clock)."""
        spec = await self._attendance_policy.spec()
        return datetime.now(UTC).astimezone(ZoneInfo(spec.timezone)).date()

    async def _enforce_not_past(self, payload: LeaveCreate) -> None:
        """Leave can't start before the backdating window opens. The window is
        `max_backdate_days` days before today (0 ⇒ no past dates at all), so HR can
        allow filing a leave that already started (e.g. yesterday's sick day).
        'Today' is the org's policy-timezone date, never the server's clock."""
        backdate = (await self._policy.get_or_create()).max_backdate_days
        earliest = await self._org_today() - timedelta(days=max(0, backdate))
        if utc_date(payload.start_date) < earliest:
            if backdate <= 0:
                raise ValidationError("You can't apply for leave for a past date.")
            raise ValidationError(
                f"Leave can be backdated at most {backdate} day{'s' if backdate != 1 else ''}."
            )

    async def _enforce_min_notice(self, payload: LeaveCreate) -> None:
        """Planned / annual leave must be applied at least `planned_min_notice_days`
        before it starts (configurable in the leave policy). Other types can be
        applied any time. 'Today' is the org's policy-timezone date.

        Notice is about warning people BEFORE you go, so it only applies to leave
        that hasn't started yet. A backdated request is governed by the backdating
        window instead (`_enforce_not_past`, already run) — without this carve-out
        the two rules contradict each other: a past date can never be "2 days in
        advance", so any backdating window at all would be unusable for planned
        and annual leave.
        """
        if payload.leave_type not in _NOTICE_TYPES:
            return
        required = (await self._policy.get_or_create()).planned_min_notice_days
        if required <= 0:
            return
        days_ahead = (utc_date(payload.start_date) - await self._org_today()).days
        if days_ahead < 0:
            return
        if days_ahead < required:
            raise ValidationError(
                f"This leave must be applied at least {required} "
                f"day{'s' if required != 1 else ''} in advance. "
                "Use sick leave if you need time off sooner."
            )

    async def _enforce_eligibility(
        self, employee: Employee, leave_type: LeaveType, start: datetime
    ) -> None:
        """Gender/DOB-gated leave: maternity (female only), paternity (male only),
        and birthday (needs a date of birth on file, and must fall in the person's
        birth month). Other types are open to everyone. Tenure eligibility is
        checked with the balance (`_enforce_balance`), which already knows it."""
        if leave_type not in (LeaveType.MATERNITY, LeaveType.PATERNITY, LeaveType.BIRTHDAY):
            return
        if leave_type is LeaveType.MATERNITY and employee.gender is not Gender.FEMALE:
            raise ValidationError("Maternity leave is available to female employees.")
        if leave_type is LeaveType.PATERNITY and employee.gender is not Gender.MALE:
            raise ValidationError("Paternity leave is available to male employees.")
        if leave_type is LeaveType.BIRTHDAY:
            if employee.date_of_birth is None:
                raise ValidationError(
                    "Add your date of birth to your profile to take birthday leave."
                )
            if utc_date(start).month != employee.date_of_birth.month:
                raise ValidationError("Birthday leave must fall in your birth month.")

    async def _enforce_balance(
        self, employee: Employee, request: _Request, *, at_approval: bool
    ) -> None:
        """Refuse a type the employee's tenure band has not granted yet, or a
        request bigger than what is left of its quota.

        Unpaid leave is uncapped and exempt. A request is split into the leave
        years it touches and each part is checked against THAT year's balance
        (`_compute_balance(on=...)`): leave after 31 March spends next year's
        quota, never nothing. Without the split a request entirely in the next
        leave year counted 0 days and was never capped.
        """
        bucket = _quota_bucket(request.leave_type)
        if bucket is None:
            return
        first, last = utc_date(request.start), utc_date(request.end)
        current = _leave_year_window(await self._org_today())
        for window in _leave_year_windows(first, last):
            await self._enforce_year_balance(
                employee,
                request,
                bucket,
                window=window,
                at_approval=at_approval,
                label_year=window != current,
            )

    async def _enforce_year_balance(
        self,
        employee: Employee,
        request: _Request,
        bucket: LeaveType,
        *,
        window: tuple[date, date],
        at_approval: bool,
        label_year: bool,
    ) -> None:
        """The balance rule for the part of a request inside one leave year.

        Reuses the exact numbers the balance panel shows (`_compute_balance`), so
        the UI warning and this refusal can never disagree. Days are working days
        (half days count 0.5).

        On apply the part must fit in `remaining` (other pending requests are
        already reserved). On approval it must fit in what is left after APPROVED
        leave only - the request itself is one of the pending ones, and the other
        pending requests may still be rejected.
        """
        balance = await self._compute_balance(employee, on=window[0])
        row = next((b for b in balance.balances if b.leave_type is bucket), None)
        if row is None:
            return
        label = _leave_type_label(bucket, None)
        year = f" for the {_leave_year_label(window)} leave year" if label_year else ""
        if not row.eligible:
            # Half-day is deliberately never withheld for tenure: it only spends
            # from the planned bucket and the leave policy does not lock it during
            # probation (see fe features/leaves/eligibility.ts). With planned
            # locked there is no quota to measure it against, so it is capped
            # per calendar month instead of being unlimited.
            if request.leave_type is not bucket:
                await self._enforce_probation_half_day_cap(
                    employee, request, at_approval=at_approval
                )
                return
            raise ValidationError(
                row.ineligible_reason or f"You are not eligible for {label} leave yet."
            )
        holidays = await self._holidays.dates_in_range(*window)
        requested = days_in_window(
            leave_type=request.leave_type,
            span=(request.start, request.end),
            window=window,
            holidays=holidays,
            working_days_per_week=(await self._attendance_policy.spec()).working_days_per_week,
        )
        available = row.allocated - row.used if at_approval else row.remaining
        if requested > available:
            raise ValidationError(
                f"Not enough {label} leave{year}: {requested:g} day"
                f"{'s' if requested != 1 else ''} requested, {max(available, 0.0):g} available."
            )

    async def _enforce_probation_half_day_cap(
        self, employee: Employee, request: _Request, *, at_approval: bool
    ) -> None:
        """Refuse a half day during probation once the month already holds
        `PROBATION_HALF_DAYS_PER_MONTH` of them.

        On apply pending and approved half days count; on approval only approved
        ones do, for the same reason as the balance rule above.
        """
        if request.leave_type is not LeaveType.HALF_DAY:
            return
        day = utc_date(request.start)
        month_start = date(day.year, day.month, 1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        statuses = (
            (LeaveStatus.APPROVED,)
            if at_approval
            else (LeaveStatus.SUBMITTED, LeaveStatus.APPROVED)
        )
        booked = await self._leaves.overlapping(
            employee.id,
            datetime(month_start.year, month_start.month, month_start.day, tzinfo=UTC),
            datetime(next_month.year, next_month.month, next_month.day, tzinfo=UTC)
            - timedelta(microseconds=1),
            statuses,
        )
        taken = sum(1 for row in booked if row.leave_type is LeaveType.HALF_DAY)
        if taken < PROBATION_HALF_DAYS_PER_MONTH:
            return
        if at_approval:
            raise ValidationError(
                f"{employee.full_name} is on probation and already has {taken} approved "
                f"half days in {day:%B %Y}; the limit is {PROBATION_HALF_DAYS_PER_MONTH} "
                "a month. Ask them to apply for unpaid leave instead."
            )
        raise ValidationError(
            f"During probation you can take up to {PROBATION_HALF_DAYS_PER_MONTH} "
            f"half days a month; {day:%B %Y} already has {taken}. "
            "Apply for unpaid leave instead."
        )

    async def _enforce_no_overlap(
        self, employee: Employee, request: _Request, *, exclude_id: uuid.UUID | None
    ) -> None:
        """Refuse leave that double-books days already covered by the employee's
        own leave (two different halves of one day are fine).

        On apply both pending and approved leave block. On approval
        (`exclude_id` set) only APPROVED leave does: another still-pending
        request is not a booking yet, and must not stop the approver deciding
        this one - whichever is approved first makes the other one clash. The
        message then speaks to the approver, not the employee.
        """
        at_approval = exclude_id is not None
        statuses = (
            (LeaveStatus.APPROVED,)
            if at_approval
            else (LeaveStatus.SUBMITTED, LeaveStatus.APPROVED)
        )
        first_day = utc_date(request.start)
        last_day = utc_date(request.end)
        existing = await self._leaves.overlapping(
            employee.id,
            datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC),
            datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC)
            + timedelta(days=1, microseconds=-1),
            statuses,
            exclude_id=exclude_id,
        )
        clash = next(
            (row for row in existing if clashes(row, request.leave_type, request.half_day)),
            None,
        )
        if clash is None:
            return
        dates = f"{clash.start_date:%d %b} - {clash.end_date:%d %b}"
        if at_approval:
            raise ValidationError(
                f"{employee.full_name} already has approved leave on {dates} "
                "that overlaps these dates."
            )
        raise ValidationError(
            f"You already have {clash.status.value} leave on {dates} that overlaps these dates."
        )

    async def _enforce_leave_rules(
        self,
        employee: Employee,
        request: _Request,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        """Eligibility, overlap and balance - run on apply and again on approval
        (`exclude_id` set), because balances and other leave move in between.

        The employee's row is locked first, so two concurrent applies/approvals
        for the same person run these checks one after the other and cannot both
        pass against the same balance (the lock is held until the commit)."""
        at_approval = exclude_id is not None
        await self._leaves.lock_employee(employee.id)
        await self._enforce_eligibility(employee, request.leave_type, request.start)
        await self._enforce_no_overlap(employee, request, exclude_id=exclude_id)
        await self._enforce_balance(employee, request, at_approval=at_approval)

    async def apply(self, caller: CurrentUser, payload: LeaveCreate) -> Leave:
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        await self._enforce_not_past(payload)
        await self._enforce_min_notice(payload)
        await self._enforce_leave_rules(employee, _Request.from_create(payload))
        # Employees apply for themselves; their manager is the default reviewer.
        leave = await self._leaves.create(
            payload, employee_id=caller.employee_id, reviewer_id=caller.manager_id
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.apply",
            target=f"leave:{leave.id}:{payload.leave_type.value}",
        )
        body = (
            f"{_leave_type_label(payload.leave_type, payload.half_day_period)} · "
            f"{payload.start_date:%d %b} - {payload.end_date:%d %b}"
        )
        reviewers = await self._reviewers_for(caller)
        for reviewer_id in reviewers:
            await self._notifications.notify(
                recipient_id=reviewer_id,
                kind=NotificationKind.LEAVE_REQUEST,
                title="Leave request to approve",
                body=body,
                link=_LEAVES_LINK,
                entity_type="leave",
                entity_id=leave.id,
                actor_id=caller.employee_id,
            )
        await self._email_request(leave, caller, reviewer_ids=set(reviewers))
        return leave

    async def _reviewers_for(self, caller: CurrentUser) -> list[uuid.UUID]:
        """Who is told about a new request: the person who actually decides it.

        That is the requester's reporting manager. HR is the fallback, not a
        second recipient on every request: when there is no manager on file, or
        the one on file has left (inactive - they can no longer sign in to act),
        the request would otherwise reach nobody who could action it. Admins are
        not told; they can neither see nor decide another person's leave.
        """
        return await self._deciders_for(caller.employee_id, caller.manager_id)

    async def _deciders_for(
        self, employee_id: uuid.UUID, manager_id: uuid.UUID | None
    ) -> list[uuid.UUID]:
        """Who decides this employee's leave right now: their active reporting
        manager, else every HR person other than the employee themselves."""
        if manager_id is not None:
            manager = await self._employees.get(manager_id)
            if manager is not None and manager.is_active:
                return [manager.id]
        return [hr.id for hr in await self._employees.list_by_role(Role.HR) if hr.id != employee_id]

    async def _email_request(
        self, leave: Leave, caller: CurrentUser, *, reviewer_ids: set[uuid.UUID]
    ) -> None:
        """Email a new leave request to the person who decides it.

        That is the requester's reporting manager, or HR when they have no
        manager on file - the same people notified in-app, so the email and the
        bell never disagree about who is meant to act. Every admin used to be
        mailed as well; they can no longer see or decide a leave request, so that
        was pure noise.

        Best-effort per recipient: one bad address must never roll back the
        request or stop the other emails, so failures are swallowed and logged.
        """
        requester = await self._employees.get(leave.employee_id)
        if requester is None:
            return
        # Never back to the requester themselves - a lead applying for their own
        # leave shouldn't email themselves.
        recipient_ids = set(reviewer_ids)
        recipient_ids.discard(leave.employee_id)

        leave_type_label = _leave_type_label(leave.leave_type, leave.half_day_period)
        date_range = f"{leave.start_date:%d %b} - {leave.end_date:%d %b}"
        for recipient_id in recipient_ids:
            recipient = await self._employees.get(recipient_id)
            if recipient is None or not recipient.is_active:
                continue
            try:
                await self._email.send_leave_request(
                    to=recipient.work_email,
                    recipient_name=recipient.full_name,
                    requester_name=requester.full_name,
                    leave_type_label=leave_type_label,
                    date_range_label=date_range,
                    reason=leave.reason,
                    can_decide=True,  # everyone mailed here is a decider now
                    link_path=_LEAVES_LINK,
                )
            except EmailError:
                logger.warning("leave_request_email_failed", extra={"leave_id": str(leave.id)})

    async def _requester_manager_id(self, leave: Leave) -> uuid.UUID | None:
        requester = await self._employees.get(leave.employee_id)
        return requester.manager_id if requester is not None else None

    async def _may_decide(self, caller: CurrentUser, leave: Leave) -> bool:
        """The requester's direct manager or HR, never your own (`may_decide`)."""
        return may_decide(caller, leave, await self._requester_manager_id(leave))

    async def decide(
        self, caller: CurrentUser, leave_id: uuid.UUID, payload: LeaveDecision
    ) -> Leave:
        # Locked: a concurrent second decision waits here, then sees this one's
        # status and gets the 409 below - never a second set of notifications.
        leave = await self._leaves.get_in_scope(caller, leave_id, for_update=True)
        if leave is None:
            raise NotFoundError()
        if not await self._may_decide(caller, leave):
            raise AuthorizationError()
        if leave.status is not LeaveStatus.SUBMITTED:
            raise ConflictError("This request can no longer be decided.")
        if payload.approve:
            requester = await self._employees.get(leave.employee_id)
            if requester is None:
                raise NotFoundError()
            await self._enforce_leave_rules(
                requester, _Request.from_leave(leave), exclude_id=leave.id
            )

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
        leave_type_label = _leave_type_label(leave.leave_type, leave.half_day_period)
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

    async def withdraw(self, caller: CurrentUser, leave_id: uuid.UUID) -> Leave:
        """The requester pulls back their own still-pending request."""
        leave = await self._leaves.get_for_update(leave_id)
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

    async def cancel_approved(
        self, caller: CurrentUser, leave_id: uuid.UUID, payload: LeaveCancel
    ) -> Leave:
        """Cancel an APPROVED leave (rules in `leave_rules.may_cancel_approved`).

        CANCELLED is not APPROVED, so the leave stops counting toward the balance
        and payroll immediately: both read approved leave only. That is why a
        month whose payslips are already released is refused: those slips are
        frozen, so the cancellation would never reach the pay it changes.
        """
        leave = await self._leaves.get_in_scope(caller, leave_id, for_update=True)
        if leave is None:
            raise NotFoundError()
        if leave.status is not LeaveStatus.APPROVED:
            raise ConflictError("Only an approved leave can be cancelled.")
        today = await self._org_today()
        if not may_cancel_approved(caller, leave, await self._requester_manager_id(leave), today):
            if caller.employee_id == leave.employee_id:
                raise ConflictError(
                    "This leave has already started. Ask HR or your manager to cancel it."
                )
            raise AuthorizationError()
        await self._enforce_payroll_open(leave)
        leave.status = LeaveStatus.CANCELLED
        await self._leaves.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.cancel",
            target=f"leave:{leave.id}",
        )
        await self._notify_cancelled(caller, leave, payload.note)
        return leave

    async def _enforce_payroll_open(self, leave: Leave) -> None:
        """Refuse to change leave in a month whose payroll is already released
        (mirrors the reimbursement settlement guard): the payslip is a frozen
        snapshot, so the pay effect has to be made as a payroll adjustment."""
        released = await self._leaves.released_payroll_months(
            _months_between(utc_date(leave.start_date), utc_date(leave.end_date))
        )
        if released:
            raise ConflictError(
                f"Payroll for {released[0]} is already released; "
                "correct it through a payroll adjustment."
            )

    async def _notify_cancelled(self, caller: CurrentUser, leave: Leave, note: str | None) -> None:
        """Tell the employee when someone else cancelled their leave, and whoever
        decides their leave NOW (their current active manager, or HR when there
        is none - `_deciders_for`) when anyone else cancelled it. Not whoever
        approved it back then: they may have moved teams or left since. Never the
        person who just did it.

        In-app only: the leave email template reads as an approve/reject
        decision, and none fits a cancellation."""
        label = _leave_type_label(leave.leave_type, leave.half_day_period)
        dates = f"{leave.start_date:%d %b} - {leave.end_date:%d %b}"
        body = f"{label} · {dates}" + (f" · {note}" if note else "")
        recipients = {
            leave.employee_id: "Your leave was cancelled",
        }
        requester = await self._employees.get(leave.employee_id)
        manager_id = requester.manager_id if requester is not None else None
        for decider_id in await self._deciders_for(leave.employee_id, manager_id):
            recipients.setdefault(decider_id, "An approved leave was cancelled")
        for recipient_id, title in recipients.items():
            if recipient_id == caller.employee_id:
                continue
            await self._notifications.notify(
                recipient_id=recipient_id,
                kind=NotificationKind.LEAVE_DECISION,
                title=title,
                body=body,
                level=NotificationLevel.WARNING,
                link=_LEAVES_LINK,
                entity_type="leave",
                entity_id=leave.id,
                actor_id=caller.employee_id,
            )

    async def permissions(
        self, caller: CurrentUser, leaves: Sequence[Leave]
    ) -> dict[uuid.UUID, LeavePermissions]:
        """What the caller may do with each leave - the same rules `decide` and
        `cancel_approved` enforce, batched so a page of rows costs one lookup."""
        requesters = await self._employees.get_many(list({lv.employee_id for lv in leaves}))
        today = await self._org_today()
        out: dict[uuid.UUID, LeavePermissions] = {}
        for leave in leaves:
            requester = requesters.get(leave.employee_id)
            manager_id = requester.manager_id if requester is not None else None
            out[leave.id] = LeavePermissions(
                can_decide=can_decide_now(caller, leave, manager_id),
                can_cancel=may_cancel_approved(caller, leave, manager_id, today),
            )
        return out

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
        result = await self._compute_balance(employee)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.balance.read",
            target=f"employee:{target_id}",
        )
        return result

    async def _compute_balance(
        self, employee: Employee, *, on: date | None = None
    ) -> LeaveBalanceRead:
        """The balance picture for one employee, without authorization or audit.

        Shared by the balance endpoint and by `_enforce_balance`, so what the UI
        shows as "remaining" and what applying/approving enforces are one number.

        `on` picks the leave year (default: the current one). Entitlement is
        evaluated as of today, clamped into that year: a FUTURE year is judged
        as it opens on 1 April - the tenure band the person will be in then, and
        one month of any monthly accrual (credited up front) - and a past year as
        it closed. Leave used and pending is what is booked inside that year.
        """
        target_id = employee.id
        anchor = employee.hire_date or employee.created_at.date()
        org_today = await self._org_today()
        year_start, year_end = _leave_year_window(on or org_today)
        as_of = min(max(org_today, year_start), year_end)
        holidays = await self._holidays.dates_in_range(year_start, year_end)
        working_days_per_week = (await self._attendance_policy.spec()).working_days_per_week
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
        allocation = await self._allocations.get_for_employee(target_id)

        # Which entitlement band this person is in today, and when they leave (or
        # left) probation. Derived from the hire date every time — never stored,
        # so it can't go stale on the day someone crosses a boundary.
        #
        # Banding uses the REAL hire date only, not the created_at fallback the
        # leave-year window uses. A missing hire date means we genuinely don't
        # know when someone joined, and guessing "today" would put long-serving
        # staff on probation and silently zero their planned leave. Unknown
        # therefore reads as fully tenured: they keep the org policy, exactly as
        # they did before tenure banding existed.
        hired_on = employee.hire_date
        # Their own negotiated probation length wins over the org default; the
        # band and the confirmation date must both use the same number or the
        # badge and the entitlement would disagree.
        probation_months = employee.probation_months or policy.probation_months
        confirmed_on = probation_end(hired_on or anchor, probation_months)
        tier = (
            tenure_status(hired_on, as_of, probation_months=probation_months)
            if hired_on is not None
            else TenureStatus.TENURED
        )
        all_rows = await self._tiers.seed_defaults()
        tier_rows = {row.leave_type: row for row in all_rows if row.tier is tier}
        # Accrual restarts at the leave-year boundary (carryforward is within the
        # year), and can't begin before the person is actually eligible.
        accrual_start = max(year_start, confirmed_on)
        accruing: list[LeaveType] = []

        def quota(leave_type: LeaveType, policy_attr: str, alloc_attr: str) -> float:
            """The employee's effective entitlement for the leave year.

            Resolution order, most specific first: their per-employee override,
            then their tenure band's rule, then the org policy default. A band row
            of 0 is a real answer ("this band gets none") — only an ABSENT row
            falls through to the policy.
            """
            override = getattr(allocation, alloc_attr) if allocation is not None else None
            if override is not None:
                return float(override)
            row = tier_rows.get(leave_type)
            if row is not None:
                if row.monthly_accrual_days is not None:
                    accruing.append(leave_type)
                    earned = accrued_units(
                        accrual_start, as_of, per_month=float(row.monthly_accrual_days)
                    )
                    # Accrual runs toward a ceiling, never past it: the band's own
                    # figure when it sets one, otherwise the org policy. Without
                    # this, "1 a month" reached 12 however few days HR had actually
                    # granted, so lowering planned leave in Settings did nothing.
                    ceiling = (
                        float(row.annual_days)
                        if row.annual_days is not None
                        else float(getattr(policy, policy_attr))
                    )
                    return min(earned, ceiling)
                if row.annual_days is not None:
                    return float(row.annual_days)
            return float(getattr(policy, policy_attr))

        def bucket(
            rows: list[tuple[datetime, datetime, LeaveType, LeaveStatus]],
            types: tuple[LeaveType, ...],
        ) -> float:
            total = 0.0
            for s, e, kind, _status in rows:
                if kind not in types:
                    continue
                ls = max(year_start, utc_date(s))
                le = min(year_end, utc_date(e))
                days = float(working_days_between(ls, le, holidays, working_days_per_week))
                total += days * leave_weight(kind)
            return total

        balances: list[LeaveTypeBalance] = []
        for leave_type, policy_attr, alloc_attr, types in _TRACKED_LEAVE:
            allocated = quota(leave_type, policy_attr, alloc_attr)
            used = bucket(approved, types)
            pend = bucket(pending, types)
            # "Not earned yet" is a statement about TENURE, so read it off the
            # band — never off a zero. A zero can equally be HR typing 0 into
            # someone's override, or a policy that grants none of a type, and
            # calling either of those "available after one year of service" is
            # nonsense to an eight-year employee looking at their own profile.
            override = allocation is not None and getattr(allocation, alloc_attr) is not None
            row = tier_rows.get(leave_type)
            not_yet = (
                not override
                and row is not None
                and row.monthly_accrual_days is None
                and row.annual_days == 0
            )
            balances.append(
                LeaveTypeBalance(
                    leave_type=leave_type,
                    allocated=allocated,
                    used=used,
                    pending=pend,
                    remaining=allocated - used - pend,
                    eligible=not not_yet,
                    ineligible_reason=(_ineligible_reason(tier, confirmed_on) if not_yet else None),
                )
            )
        # Unpaid leave is uncapped — surfaced for visibility (used/pending only).
        unpaid_used = bucket(approved, (LeaveType.UNPAID,))
        unpaid_pending = bucket(pending, (LeaveType.UNPAID,))
        balances.append(
            LeaveTypeBalance(
                leave_type=LeaveType.UNPAID,
                allocated=0.0,
                used=unpaid_used,
                pending=unpaid_pending,
                remaining=-unpaid_used - unpaid_pending,
            )
        )

        return LeaveBalanceRead(
            leave_year_start=year_start,
            leave_year_end=year_end,
            balances=balances,
            tenure_status=tier,
            probation_end_date=confirmed_on,
            accruing_types=accruing,
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


# Display names that don't follow the "title-case the enum value" rule. Bereavement
# is granted specifically for the loss of a parent, so it is named for that rather
# than reading as general compassionate leave.
_LEAVE_TYPE_LABELS: dict[LeaveType, str] = {
    LeaveType.BEREAVEMENT: "Parent Bereavement",
}


def _leave_type_label(leave_type: LeaveType, half_day_period: HalfDayPeriod | None) -> str:
    label = _LEAVE_TYPE_LABELS.get(leave_type, leave_type.value.replace("_", " ").title())
    if leave_type is LeaveType.HALF_DAY and half_day_period is not None:
        return f"{label} ({half_day_period.value.replace('_', ' ').title()})"
    return label


def _ineligible_reason(tier: TenureStatus, confirmed_on: date) -> str:
    """Why this leave type is withheld, in words the employee can act on.

    Only reached when the person's TENURE BAND grants none of the type, so the
    reason is always about tenure. The tenured band withholds nothing, so it
    should never arrive here — if it somehow does, say nothing about years of
    service, because the reader has already served them.
    """
    if tier is TenureStatus.PROBATION:
        return f"Available once you complete probation on {confirmed_on:%d %b %Y}"
    if tier is TenureStatus.CONFIRMED:
        return "Available after one year of service"
    return "Not available on your current plan"


# The company leave year is the Indian financial year (Leave Policy 2026, General
# Guidelines: "the financial year from 1 April to 31 March"). Everyone resets
# together on 1 April, rather than each person resetting on their own joining
# anniversary as they used to.
LEAVE_YEAR_START_MONTH = 4


def _leave_year_window(today: date) -> tuple[date, date]:
    """The financial-year leave window containing `today`: 1 April - 31 March.

    Returns [start, end] inclusive. January-March belong to the year that STARTED
    the previous April, so February 2027 sits in the 2026-27 window.
    """
    year = today.year if today.month >= LEAVE_YEAR_START_MONTH else today.year - 1
    start = date(year, LEAVE_YEAR_START_MONTH, 1)
    return start, date(year + 1, LEAVE_YEAR_START_MONTH, 1) - timedelta(days=1)


def _leave_year_windows(first: date, last: date) -> list[tuple[date, date]]:
    """Every leave-year window the inclusive range [first, last] touches, in
    order - one for a request inside a year, two for one spanning 31 March."""
    windows: list[tuple[date, date]] = []
    day = first
    while day <= last:
        window = _leave_year_window(day)
        windows.append(window)
        day = window[1] + timedelta(days=1)
    return windows


def _leave_year_label(window: tuple[date, date]) -> str:
    """'2027-28' for the 1 Apr 2027 - 31 Mar 2028 leave year."""
    return f"{window[0].year}-{window[1].year % 100:02d}"


def _months_between(first: date, last: date) -> list[str]:
    """Every payroll month (`YYYY-MM`) the inclusive range [first, last] touches."""
    months: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months
