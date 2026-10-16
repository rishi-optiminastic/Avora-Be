"""Pure leave rules shared by the service and its read models.

Who may decide or cancel a leave, and how many working days a request spends,
live here as plain functions with no I/O. The service enforces them on writes
and the list/read endpoints report them as `can_decide` / `can_cancel`, so the
UI renders exactly what the server will allow - the two can never drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from app.core.payroll import working_days_between
from app.models.employee import Role
from app.models.leave import HalfDayPeriod, Leave, LeaveStatus, LeaveType
from app.schemas.auth import CurrentUser

# A half-day leave consumes half a working day; full-day types consume one.
HALF_DAY_WEIGHT = 0.5


def utc_date(value: datetime) -> date:
    """The UTC calendar date of a stored leave bound. Leaves are persisted at UTC
    midnight; a naive value (e.g. from SQLite) is treated as already-UTC rather
    than shifted by the machine's local timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def may_decide(caller: CurrentUser, leave: Leave, requester_manager_id: uuid.UUID | None) -> bool:
    """Who may approve or reject a leave request.

    The requester's own reporting manager - the person who actually knows
    whether the team can spare them that week - or HR.

    NOT admin, unless they happen to be that manager. A leave request goes to
    the person you report to; running the workspace is not a reason to decide
    it (org decision), and admin can no longer see these requests either.

    HR stays because it administers leave, and because a request from someone
    with no reporting manager on file has to land somewhere.

    Never your own request, whatever your role (segregation of duties).
    """
    if caller.employee_id == leave.employee_id:
        return False
    if caller.role is Role.HR:
        return True
    return requester_manager_id is not None and requester_manager_id == caller.employee_id


def can_decide_now(
    caller: CurrentUser, leave: Leave, requester_manager_id: uuid.UUID | None
) -> bool:
    """`may_decide`, and the request is still waiting for a decision."""
    return leave.status is LeaveStatus.SUBMITTED and may_decide(caller, leave, requester_manager_id)


def may_cancel_approved(
    caller: CurrentUser,
    leave: Leave,
    requester_manager_id: uuid.UUID | None,
    today: date,
) -> bool:
    """Who may cancel an APPROVED leave.

    - The employee themselves, but only before it starts: once a day of it has
      been taken, undoing it would rewrite attendance and pay after the fact.
    - HR, or the requester's direct manager, at any time (e.g. someone came back
      early, or the leave was approved by mistake).

    The owner rule wins when the caller is also HR: an HR person cancelling their
    own leave is the employee, not the administrator.
    """
    if leave.status is not LeaveStatus.APPROVED:
        return False
    if caller.employee_id == leave.employee_id:
        return utc_date(leave.start_date) > today
    if caller.role is Role.HR:
        return True
    return requester_manager_id is not None and requester_manager_id == caller.employee_id


def leave_weight(leave_type: LeaveType) -> float:
    return HALF_DAY_WEIGHT if leave_type is LeaveType.HALF_DAY else 1.0


def days_in_window(
    *,
    leave_type: LeaveType,
    span: tuple[datetime, datetime],
    window: tuple[date, date],
    holidays: set[date],
    working_days_per_week: int,
) -> float:
    """Working days a leave spends inside an inclusive [start, end] date window,
    half days counting `HALF_DAY_WEIGHT`. Zero when it falls outside."""
    start = max(window[0], utc_date(span[0]))
    end = min(window[1], utc_date(span[1]))
    days = float(working_days_between(start, end, holidays, working_days_per_week))
    return days * leave_weight(leave_type)


def clashes(existing: Leave, leave_type: LeaveType, half_day_period: HalfDayPeriod | None) -> bool:
    """Whether an overlapping existing leave really collides with a new one.

    The date ranges already overlap; the one exception is two half days on the
    same date in DIFFERENT halves - a morning off and an afternoon off are two
    legitimate requests, not a double booking.
    """
    if (
        existing.leave_type is LeaveType.HALF_DAY
        and leave_type is LeaveType.HALF_DAY
        and existing.half_day_period is not None
        and half_day_period is not None
    ):
        return existing.half_day_period is half_day_period
    return True
