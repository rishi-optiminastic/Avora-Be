"""Centralised-dashboard rollups — delayed tasks, department performance, and
project-wise manpower. Every read is scoped to the caller's visible set
(Security rule 5.3): tasks via the task scope clause, people/projects via
`all_in_scope`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta

from app.models.task import Task
from app.repositories.activity import ActivityRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.dashboard import DepartmentStat, ProjectManpowerStat


class DashboardService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        activity: ActivityRepository,
        entities: WorkEntityRepository,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._activity = activity
        self._entities = entities

    async def delayed_tasks(self, caller: CurrentUser, now: datetime) -> Sequence[Task]:
        return await self._tasks.list_overdue(caller, now)

    async def department_performance(
        self, caller: CurrentUser, day: datetime
    ) -> list[DepartmentStat]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start = datetime.combine(day.date(), time.min, tzinfo=UTC)
        aggs = await self._activity.daily_aggregates(ids, start, start + timedelta(days=1))

        # Accumulate per department: headcount, present today, productivity sum.
        acc: dict[str | None, list[int]] = {}  # dept -> [head, present, prod_sum]
        for e in employees:
            bucket = acc.setdefault(e.department, [0, 0, 0])
            bucket[0] += 1
            agg = aggs.get(e.id)
            if agg is None:
                continue
            worked = max(0, int((agg.logout_at - agg.login_at).total_seconds() // 60))
            active = max(0, worked - min(worked, agg.idle_seconds // 60))
            bucket[1] += 1
            bucket[2] += round((active / worked) * 100) if worked else 0

        return [
            DepartmentStat(
                department=dept,
                headcount=head,
                present_today=present,
                avg_productivity_pct=round(prod_sum / present) if present else 0,
            )
            for dept, (head, present, prod_sum) in sorted(
                acc.items(), key=lambda kv: (kv[0] is None, kv[0] or "")
            )
        ]

    async def project_manpower(self, caller: CurrentUser) -> list[ProjectManpowerStat]:
        rows = await self._tasks.manpower_by_project(caller)
        names = {e.id: e.name for e in await self._entities.list_all()}
        return [
            ProjectManpowerStat(
                project_id=r.project_id,
                project_name=names.get(r.project_id),
                people=r.people,
                open_tasks=r.open_tasks,
                total_tasks=r.total_tasks,
            )
            for r in sorted(rows, key=lambda r: r.open_tasks, reverse=True)
        ]
