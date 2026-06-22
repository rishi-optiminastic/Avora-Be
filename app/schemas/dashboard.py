"""Centralised-dashboard rollup schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel

LiveStatus = Literal["active", "idle", "offline"]


class OverviewPerson(BaseModel):
    """One person's live row for the Overview hero (pulse + seat map + live-now)."""

    employee_id: uuid.UUID
    name: str
    department: str | None
    job_title: str | None
    status: LiveStatus
    productivity_pct: int
    active_window: str | None  # what they're working on right now


class OverviewRead(BaseModel):
    """Everything the Overview hero needs in one scoped round trip — derived from
    live activity, today's attendance, task counts, and approved leave."""

    people: list[OverviewPerson]
    total: int
    active: int
    idle: int
    offline: int
    avg_productivity_pct: int  # average over active people with productivity > 0
    present_today: int
    on_leave: int
    tasks_done: int
    tasks_total: int


class DepartmentStat(BaseModel):
    """Department-wise performance summary for the caller's scope."""

    department: str | None
    headcount: int
    present_today: int
    avg_productivity_pct: int


class ProjectManpowerStat(BaseModel):
    """How many people + tasks a project is carrying (project-wise manpower)."""

    project_id: uuid.UUID
    project_name: str | None
    people: int
    open_tasks: int
    total_tasks: int
