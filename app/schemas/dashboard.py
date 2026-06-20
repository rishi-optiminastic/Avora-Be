"""Centralised-dashboard rollup schemas (Golden rule #5)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


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
