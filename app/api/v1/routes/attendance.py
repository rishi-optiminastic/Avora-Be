"""Attendance endpoints — derived from raw activity, scoped to the caller."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, MonitoringServiceDep, WorkSessionServiceDep
from app.schemas.monitoring import AttendanceRead
from app.schemas.work_session import WorkSessionRead

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("", response_model=list[AttendanceRead])
async def list_attendance(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[AttendanceRead]:
    """Per-employee attendance for a day (defaults to today)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    return await service.attendance(caller, when)


@router.get("/me", response_model=WorkSessionRead | None)
async def my_session(
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead | None:
    """The caller's open work session (or null if not clocked in)."""
    session = await service.current(caller)
    return WorkSessionRead.model_validate(session) if session else None


@router.post("/clock-in", response_model=WorkSessionRead, status_code=status.HTTP_201_CREATED)
async def clock_in(
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead:
    """Start the work day (idempotent — returns the open session if already in)."""
    session = await service.clock_in(caller)
    return WorkSessionRead.model_validate(session)


@router.post("/clock-out", response_model=WorkSessionRead)
async def clock_out(
    caller: CurrentUserDep,
    service: WorkSessionServiceDep,
) -> WorkSessionRead:
    """End the work day (closes the open session; 422 if not clocked in)."""
    return WorkSessionRead.model_validate(await service.clock_out(caller))
