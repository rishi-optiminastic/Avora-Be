"""Centralised-dashboard rollup endpoints (scoped to the caller)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, DashboardServiceDep
from app.schemas.dashboard import DepartmentStat, ProjectManpowerStat
from app.schemas.task import TaskRead

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/delayed-tasks", response_model=list[TaskRead])
async def delayed_tasks(
    caller: CurrentUserDep,
    service: DashboardServiceDep,
) -> list[TaskRead]:
    """Overdue, not-done tasks in the caller's scope."""
    rows = await service.delayed_tasks(caller, datetime.now(UTC))
    return [TaskRead.model_validate(t) for t in rows]


@router.get("/department-performance", response_model=list[DepartmentStat])
async def department_performance(
    caller: CurrentUserDep,
    service: DashboardServiceDep,
) -> list[DepartmentStat]:
    """Headcount, present-today, and average productivity per department."""
    return await service.department_performance(caller, datetime.now(UTC))


@router.get("/project-manpower", response_model=list[ProjectManpowerStat])
async def project_manpower(
    caller: CurrentUserDep,
    service: DashboardServiceDep,
) -> list[ProjectManpowerStat]:
    """People + task load carried by each project."""
    return await service.project_manpower(caller)
