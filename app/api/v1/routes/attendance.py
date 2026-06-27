"""Attendance endpoints — policy-aware reads, clock in/out, office-timings
policy, and regularization, all scoped to the caller."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.core.deps import (
    AttendancePolicyServiceDep,
    AttendanceServiceDep,
    CurrentUserDep,
    ReconciliationServiceDep,
    RegularizationServiceDep,
    WorkSessionServiceDep,
)
from app.models.regularization import RegularizationStatus
from app.schemas.attendance_policy import AttendancePolicyRead, AttendancePolicyUpdate
from app.schemas.attendance_report import AttendanceDayRow, AttendanceMonthSummary
from app.schemas.monitoring import AttendanceRead
from app.schemas.reconciliation import ReconciliationReport
from app.schemas.regularization import (
    RegularizationCreate,
    RegularizationRead,
    RegularizationReview,
)
from app.schemas.work_session import BiometricTodayRead, WorkSessionRead

router = APIRouter(prefix="/attendance", tags=["attendance"])

_MONTH = r"^\d{4}-\d{2}$"


# --------------------------------------------------------------------------- #
# Reads — single day, date range, per employee, monthly report
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[AttendanceRead])
async def list_attendance(
    caller: CurrentUserDep,
    service: AttendanceServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[AttendanceRead]:
    """Per-employee attendance for a day (defaults to today)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    return await service.daily(caller, when)


@router.get("/range", response_model=list[AttendanceDayRow])
async def attendance_range(
    caller: CurrentUserDep,
    service: AttendanceServiceDep,
    start: date,
    end: date,
    employee_id: uuid.UUID | None = None,
) -> list[AttendanceDayRow]:
    """Per-employee-per-day attendance across a date range (scoped)."""
    return await service.range(caller, start.isoformat(), end.isoformat(), employee_id)


@router.get("/employee/{employee_id}", response_model=list[AttendanceDayRow])
async def employee_attendance(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: AttendanceServiceDep,
    start: date,
    end: date,
) -> list[AttendanceDayRow]:
    """One employee's attendance across a date range (404 if out of scope)."""
    return await service.range(caller, start.isoformat(), end.isoformat(), employee_id)


@router.get("/report", response_model=list[AttendanceMonthSummary])
async def monthly_report(
    caller: CurrentUserDep,
    service: AttendanceServiceDep,
    month: Annotated[str, Query(pattern=_MONTH)],
) -> list[AttendanceMonthSummary]:
    """Per-employee monthly attendance summary (YYYY-MM)."""
    return await service.monthly_report(caller, month)


@router.get("/reconciliation", response_model=ReconciliationReport)
async def reconciliation(
    caller: CurrentUserDep,
    service: ReconciliationServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> ReconciliationReport:
    """Biometric punches vs laptop-agent activity for a day (defaults to today)."""
    return await service.for_day(caller, day.isoformat() if day else None)


# --------------------------------------------------------------------------- #
# Office-timings policy (read: anyone · write: admin/HR)
# --------------------------------------------------------------------------- #
@router.get("/policy", response_model=AttendancePolicyRead)
async def get_policy(
    caller: CurrentUserDep,
    service: AttendancePolicyServiceDep,
) -> AttendancePolicyRead:
    return AttendancePolicyRead.from_model(await service.get_or_create())


@router.put("/policy", response_model=AttendancePolicyRead)
async def update_policy(
    payload: AttendancePolicyUpdate,
    caller: CurrentUserDep,
    service: AttendancePolicyServiceDep,
) -> AttendancePolicyRead:
    return AttendancePolicyRead.from_model(await service.update(caller, payload))


# --------------------------------------------------------------------------- #
# Clock in / out (self-service)
# --------------------------------------------------------------------------- #
@router.get("/me", response_model=WorkSessionRead | None)
async def my_session(
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead | None:
    """The caller's open work session (or null if not clocked in)."""
    session = await service.current(caller)
    return WorkSessionRead.model_validate(session) if session else None


@router.get("/me/today", response_model=BiometricTodayRead | None)
async def my_biometric_today(
    caller: CurrentUserDep,
    service: AttendanceServiceDep,
) -> BiometricTodayRead | None:
    """The caller's biometric check-in for today (drives the navbar timer)."""
    return await service.my_biometric_today(caller)


@router.post("/clock-in", response_model=WorkSessionRead, status_code=status.HTTP_201_CREATED)
async def clock_in(
    request: Request,
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead:
    """Start the work day (idempotent — returns the open session if already in)."""
    client_ip = request.client.host if request.client else None
    session = await service.clock_in(caller, ip_address=client_ip)
    return WorkSessionRead.model_validate(session)


@router.post("/clock-out", response_model=WorkSessionRead)
async def clock_out(
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead:
    """End the work day (closes the open session; 422 if not clocked in)."""
    return WorkSessionRead.model_validate(await service.clock_out(caller))


# --------------------------------------------------------------------------- #
# Regularization (request: self · review: manager/HR)
# --------------------------------------------------------------------------- #
@router.post(
    "/regularizations", response_model=RegularizationRead, status_code=status.HTTP_201_CREATED
)
async def request_regularization(
    payload: RegularizationCreate,
    caller: CurrentUserDep,
    service: RegularizationServiceDep,
) -> RegularizationRead:
    """Request that a late day count as full (consumes a credit on approval)."""
    return RegularizationRead.model_validate(await service.request(caller, payload))


@router.get("/regularizations", response_model=list[RegularizationRead])
async def list_regularizations(
    caller: CurrentUserDep,
    service: RegularizationServiceDep,
    month: Annotated[str | None, Query(pattern=_MONTH)] = None,
    reg_status: Annotated[RegularizationStatus | None, Query(alias="status")] = None,
) -> list[RegularizationRead]:
    """Regularizations in the caller's scope (own; managers see their reports')."""
    rows = await service.list_for_caller(caller, month=month, status=reg_status)
    return [RegularizationRead.model_validate(r) for r in rows]


@router.post("/regularizations/{reg_id}/review", response_model=RegularizationRead)
async def review_regularization(
    reg_id: uuid.UUID,
    payload: RegularizationReview,
    caller: CurrentUserDep,
    service: RegularizationServiceDep,
) -> RegularizationRead:
    """Approve/reject a regularization (manager/HR, scoped, not your own)."""
    return RegularizationRead.model_validate(await service.review(caller, reg_id, payload))
