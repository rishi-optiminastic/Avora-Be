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
from app.repositories.leave import LeaveRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser
from app.schemas.dashboard import (
    DepartmentStat,
    OverviewPerson,
    OverviewRead,
    ProjectManpowerStat,
)
from app.schemas.monitoring import AttendanceStatus
from app.services.attendance_service import AttendanceService
from app.services.monitoring_service import MonitoringService


class DashboardService:
    def __init__(
        self,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        activity: ActivityRepository,
        entities: WorkEntityRepository,
        leaves: LeaveRepository,
        attendance: AttendanceService,
        monitoring: MonitoringService,
    ) -> None:
        self._tasks = tasks
        self._employees = employees
        self._activity = activity
        self._entities = entities
        self._leaves = leaves
        self._attendance = attendance
        self._monitoring = monitoring

    async def overview(self, caller: CurrentUser, now: datetime) -> OverviewRead:
        """The Overview hero in one call: live status + today's productivity per
        person, plus present / leave / task headline counts — all caller-scoped.

        Reuses the monitoring (live status) and attendance (today's productivity +
        present/absent classification) services so the thresholds and policy logic
        live in exactly one place.
        """
        # Resolve the scope once and hand it to both composed services so they
        # don't each re-query the same employee set (was 3x the lookup).
        employees = await self._employees.all_in_scope(caller)
        live_rows = await self._monitoring.live_now(caller, now, employees)
        att_rows = await self._attendance.daily(caller, now, employees)
        live = {r.employee_id: r for r in live_rows}
        att = {r.employee_id: r for r in att_rows}

        people: list[OverviewPerson] = []
        active = idle = offline = present = prod_sum = prod_n = 0
        for e in employees:
            lv = live.get(e.id)
            at = att.get(e.id)
            if lv is not None and lv.online:
                status = "idle" if lv.idle else "active"
            else:
                status = "offline"
            prod = at.productivity_pct if at is not None else 0
            if status == "active":
                active += 1
                if prod > 0:
                    prod_sum += prod
                    prod_n += 1
            elif status == "idle":
                idle += 1
            else:
                offline += 1
            if at is not None and at.status is not AttendanceStatus.ABSENT:
                present += 1
            people.append(
                OverviewPerson(
                    employee_id=e.id,
                    name=e.full_name,
                    department=e.department,
                    job_title=e.job_title,
                    status=status,
                    productivity_pct=prod,
                    active_window=lv.active_window if lv is not None else None,
                )
            )

        ids = [e.id for e in employees]
        start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        on_leave = await self._leaves.count_on_leave(ids, start, start + timedelta(days=1))
        tasks_done, tasks_total = await self._tasks.count_done_total(caller)

        return OverviewRead(
            people=people,
            total=len(employees),
            active=active,
            idle=idle,
            offline=offline,
            avg_productivity_pct=round(prod_sum / prod_n) if prod_n else 0,
            present_today=present,
            on_leave=on_leave,
            tasks_done=tasks_done,
            tasks_total=tasks_total,
        )

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
