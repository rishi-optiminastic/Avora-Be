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
from app.models.employee import Gender, Role
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
    LeaveCommentCreate,
    LeaveCreate,
    LeaveDecision,
    LeaveTypeBalance,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.email_service import EmailError, EmailService
from app.services.leave_policy_service import LeavePolicyService
from app.services.notification_service import NotificationService

# A half-day leave consumes half a working day; full-day types consume one.
_HALF_DAY_WEIGHT = 0.5
# Half-day time off draws from the planned-leave quota (it is planned time off).
_PLANNED_TYPES = (LeaveType.PLANNED, LeaveType.HALF_DAY)

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
        if _utc_date(payload.start_date) < earliest:
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
        days_ahead = (_utc_date(payload.start_date) - await self._org_today()).days
        if days_ahead < 0:
            return
        if days_ahead < required:
            raise ValidationError(
                f"This leave must be applied at least {required} "
                f"day{'s' if required != 1 else ''} in advance. "
                "Use sick leave if you need time off sooner."
            )

    async def _enforce_eligibility(self, caller: CurrentUser, payload: LeaveCreate) -> None:
        """Gender/DOB-gated leave: maternity (female only), paternity (male only),
        and birthday (needs a date of birth on file, and must fall in the person's
        birth month). Other types are open to everyone."""
        leave_type = payload.leave_type
        if leave_type not in (LeaveType.MATERNITY, LeaveType.PATERNITY, LeaveType.BIRTHDAY):
            return
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        if leave_type is LeaveType.MATERNITY and employee.gender is not Gender.FEMALE:
            raise ValidationError("Maternity leave is available to female employees.")
        if leave_type is LeaveType.PATERNITY and employee.gender is not Gender.MALE:
            raise ValidationError("Paternity leave is available to male employees.")
        if leave_type is LeaveType.BIRTHDAY:
            if employee.date_of_birth is None:
                raise ValidationError(
                    "Add your date of birth to your profile to take birthday leave."
                )
            if _utc_date(payload.start_date).month != employee.date_of_birth.month:
                raise ValidationError("Birthday leave must fall in your birth month.")

    async def apply(self, caller: CurrentUser, payload: LeaveCreate) -> Leave:
        await self._enforce_not_past(payload)
        await self._enforce_eligibility(caller, payload)
        await self._enforce_min_notice(payload)
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
            f"{_leave_type_label(payload.leave_type, payload.half_day_period)} · "
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
        await self._email_request(leave, caller, admin_ids=admin_ids)
        return leave

    async def _email_request(
        self, leave: Leave, caller: CurrentUser, *, admin_ids: set[uuid.UUID]
    ) -> None:
        """Email a new leave request to the people who hear about it in-app: the
        admins who can decide it, plus the requester's reporting manager.

        The manager's copy is explicitly framed as visibility, not an action —
        approval is admin-only (see `decide`), so telling a manager to "approve"
        would send them after a button they don't have.

        Best-effort per recipient: one bad address must never roll back the
        request or stop the other emails, so failures are swallowed and logged.
        """
        requester = await self._employees.get(leave.employee_id)
        if requester is None:
            return
        # Deliver once per person, and never back to the requester themselves —
        # an admin applying for their own leave shouldn't email themselves.
        recipient_ids = set(admin_ids)
        if caller.manager_id is not None:
            recipient_ids.add(caller.manager_id)
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
                    can_decide=recipient_id in admin_ids,
                    link_path=_LEAVES_LINK,
                )
            except EmailError:
                logger.warning("leave_request_email_failed", extra={"leave_id": str(leave.id)})

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
        year_start, year_end = _leave_year_window(datetime.now(UTC).date())
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
        org_today = await self._org_today()
        hired_on = employee.hire_date
        # Their own negotiated probation length wins over the org default; the
        # band and the confirmation date must both use the same number or the
        # badge and the entitlement would disagree.
        probation_months = employee.probation_months or policy.probation_months
        confirmed_on = probation_end(hired_on or anchor, probation_months)
        tier = (
            tenure_status(hired_on, org_today, probation_months=probation_months)
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
            """The employee's effective entitlement for the current leave year.

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
                        accrual_start, org_today, per_month=float(row.monthly_accrual_days)
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
                ls = max(year_start, _utc_date(s))
                le = min(year_end, _utc_date(e))
                days = float(working_days_between(ls, le, holidays, working_days_per_week))
                total += days * (_HALF_DAY_WEIGHT if kind is LeaveType.HALF_DAY else 1.0)
            return total

        balances: list[LeaveTypeBalance] = []
        for leave_type, policy_attr, alloc_attr, types in _TRACKED_LEAVE:
            allocated = quota(leave_type, policy_attr, alloc_attr)
            used = bucket(approved, types)
            pend = bucket(pending, types)
            # A band that grants none of a type has not *run out* of it — the
            # person has not earned it yet. Both read as 0 remaining, so say
            # which, or the UI can only show an unexplained zero.
            not_yet = allocated == 0 and used == 0 and pend == 0
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

        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave.balance.read",
            target=f"employee:{target_id}",
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


def _utc_date(value: datetime) -> date:
    """The UTC calendar date of a stored leave bound. Leaves are persisted at UTC
    midnight; a naive value (e.g. from SQLite) is treated as already-UTC rather
    than shifted by the machine's local timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def _ineligible_reason(tier: TenureStatus, confirmed_on: date) -> str:
    """Why this leave type is at zero, in words the employee can act on.

    Only reached for a type the person's band grants none of, so the reason is
    always about tenure — either probation is still running, or the type needs a
    full year of service.
    """
    if tier is TenureStatus.PROBATION:
        return f"Available once you complete probation on {confirmed_on:%d %b %Y}"
    return "Available after one year of service"


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
