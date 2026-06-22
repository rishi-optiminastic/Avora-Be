"""End-of-Day report business rules.

Generate (system/admin) → employee reviews/edits → approve → email manager+admins,
with a cutoff auto-send for unreviewed drafts. Absent employees get no report.
Reads are scoped via the employee scope (`can_read`); edit/approve are author-only.
No FastAPI objects here (Layering §4); the screenshot signal is OCR *text* only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.eod_report import EodReport, EodStatus
from app.models.notification import NotificationKind
from app.models.task import Task, TaskStatus
from app.repositories.activity import ActivityRepository, DailyAgg
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.schemas.auth import CurrentUser
from app.schemas.eod import EodDraftContent, EodReportRead
from app.schemas.monitoring import AttendanceStatus
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.email_templates import eod_report_email
from app.services.llm_service import LlmError, LlmService
from app.services.notification_service import NotificationService

logger = get_logger("app.eod")

# Non-human admin caller for the scheduler (whole active org; sentinel id is
# never matched on — admin's scope clause ignores employee_id). Mirrors payroll.
SYSTEM_CALLER = CurrentUser(employee_id=uuid.UUID(int=0), role=Role.ADMIN, manager_id=None)

_EOD_LINK = "/dashboard/me/eod"
_ACTIVE_STATUSES = (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)


class EodService:
    def __init__(
        self,
        reports: EodReportRepository,
        employees: EmployeeRepository,
        tasks: TaskRepository,
        activity: ActivityRepository,
        screenshots: ScreenshotRepository,
        attendance: AttendanceService,
        policy: AttendancePolicyService,
        llm: LlmService,
        email: EmailService,
        notifications: NotificationService,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._reports = reports
        self._employees = employees
        self._tasks = tasks
        self._activity = activity
        self._screenshots = screenshots
        self._attendance = attendance
        self._policy = policy
        self._llm = llm
        self._email = email
        self._notifications = notifications
        self._audit = audit
        self._settings = settings

    # ---- reads (scoped) ---------------------------------------------------- #
    async def today_for(self, caller: CurrentUser, now: datetime) -> EodReportRead | None:
        report_date = await self._local_date(now)
        report = await self._reports.get_for_day(caller.employee_id, report_date)
        return EodReportRead.from_model(report) if report is not None else None

    async def get_for_caller(self, caller: CurrentUser, report_id: uuid.UUID) -> EodReportRead:
        report = await self._reports.get(report_id)
        # 404 (not 403) out-of-scope so we never reveal a report exists (§7).
        if report is None or not await self._employees.can_read(caller, report.employee_id):
            raise NotFoundError()
        return EodReportRead.from_model(report)

    async def list_for_scope(
        self, caller: CurrentUser, now: datetime, report_date: str | None
    ) -> list[EodReportRead]:
        day = report_date or await self._local_date(now)
        employees = await self._employees.all_in_scope(caller)
        reports = await self._reports.list_for_employees([e.id for e in employees], day)
        return [EodReportRead.from_model(r) for r in reports]

    # ---- employee edit + approve ------------------------------------------ #
    async def update_draft(
        self, caller: CurrentUser, report_id: uuid.UUID, summary: str
    ) -> EodReportRead:
        report = await self._own_draft(caller, report_id)
        report.edited_summary = summary
        await self._reports.flush()
        return EodReportRead.from_model(report)

    async def approve(self, caller: CurrentUser, report_id: uuid.UUID) -> EodReportRead:
        report = await self._own_draft(caller, report_id)
        report.status = EodStatus.APPROVED
        report.approved_at = datetime.now(UTC)
        report.approved_by = caller.employee_id
        await self._send(report)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="eod.approve",
            target=f"eod:{report.id}",
        )
        return EodReportRead.from_model(report)

    async def _own_draft(self, caller: CurrentUser, report_id: uuid.UUID) -> EodReport:
        report = await self._reports.get(report_id)
        if report is None or report.employee_id != caller.employee_id:
            raise NotFoundError()
        if report.status is not EodStatus.DRAFT:
            raise ConflictError("This report has already been sent.")
        return report

    # ---- generation (system/admin) ---------------------------------------- #
    async def generate_for_day(self, caller: CurrentUser, now: datetime) -> int:
        """Generate a draft for every present, not-yet-generated employee in scope.
        Returns the number of drafts created. Idempotent per (employee, date)."""
        if caller.role is not Role.ADMIN:
            raise AuthorizationError()
        report_date = await self._local_date(now)
        start, end = await self._day_bounds(now)
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        already = {r.employee_id for r in await self._reports.list_for_employees(ids, report_date)}
        attendance = {a.employee_id: a.status for a in await self._attendance.daily(caller, now)}

        created = 0
        for employee in employees:
            if employee.id in already:
                continue
            status = attendance.get(employee.id)
            if status is None or status is AttendanceStatus.ABSENT:
                await self._reports.create(
                    employee_id=employee.id,
                    report_date=report_date,
                    status=EodStatus.SKIPPED_ABSENT,
                )
                continue
            if await self._generate_one(caller, employee, report_date, start, end):
                created += 1
        return created

    async def _generate_one(
        self,
        caller: CurrentUser,
        employee: Employee,
        report_date: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        context = await self._build_context(caller, employee, start, end)
        try:
            draft = await self._llm.generate_eod(context)
        except LlmError as exc:
            logger.warning("eod generation failed for %s: %s", employee.id, exc)
            await self._reports.create(
                employee_id=employee.id,
                report_date=report_date,
                status=EodStatus.FAILED,
                error=str(exc)[:512],
                model=self._settings.eod_model,
            )
            return False
        report = await self._reports.create(
            employee_id=employee.id,
            report_date=report_date,
            status=EodStatus.DRAFT,
            summary=draft.summary,
            highlights=draft.model_dump(),
            model=self._settings.eod_model,
        )
        await self._notifications.notify(
            recipient_id=employee.id,
            kind=NotificationKind.SYSTEM,
            title="Your End-of-Day report is ready to review",
            body="Review and approve today's summary before it goes to your manager.",
            link=_EOD_LINK,
            entity_type="eod_report",
            entity_id=report.id,
        )
        return True

    async def auto_send_overdue(self, now: datetime) -> int:
        """Send drafts left unreviewed past the cutoff, as-is. Returns count sent."""
        cutoff = now - timedelta(hours=self._settings.eod_auto_send_after_hours)
        overdue = await self._reports.list_overdue_drafts(cutoff)
        sent = 0
        for report in overdue:
            report.status = EodStatus.APPROVED
            report.approved_at = now
            await self._send(report)
            sent += 1
        return sent

    # ---- send -------------------------------------------------------------- #
    async def _send(self, report: EodReport) -> None:
        employee = await self._employees.get(report.employee_id)
        if employee is None:
            report.status = EodStatus.SENT  # employee gone — close it out
            report.sent_at = datetime.now(UTC)
            await self._reports.flush()
            return
        recipients = await self._recipients(employee)
        if recipients:
            subject, html = eod_report_email(
                employee_name=employee.full_name,
                date_label=report.report_date,
                summary=report.edited_summary or report.summary,
                highlights=EodDraftContent.model_validate(report.highlights or {}),
            )
            for recipient in recipients:
                await self._email.send(to=recipient, subject=subject, html=html)
        report.status = EodStatus.SENT
        report.sent_at = datetime.now(UTC)
        await self._reports.flush()
        await self._audit.append(
            actor="system:eod",
            action="eod.send",
            target=f"eod:{report.id}",
        )

    async def _recipients(self, employee: Employee) -> list[str]:
        emails: list[str] = []
        if employee.manager_id is not None:
            manager = await self._employees.get(employee.manager_id)
            if manager is not None and manager.is_active:
                emails.append(manager.work_email)
        for admin in await self._employees.list_by_role(Role.ADMIN):
            emails.append(admin.work_email)
        # de-dup, drop the employee's own address
        return [e for e in dict.fromkeys(emails) if e and e != employee.work_email]

    # ---- context + time helpers ------------------------------------------- #
    async def _build_context(
        self, caller: CurrentUser, employee: Employee, start: datetime, end: datetime
    ) -> str:
        tasks, _ = await self._tasks.list_for_scope(
            caller, offset=0, limit=200, assignee_id=employee.id
        )
        completed = [
            t.title
            for t in tasks
            if t.status is TaskStatus.DONE and _in_range(t.completed_at, start, end)
        ]
        active = [
            t.title for t in tasks if t.status in _ACTIVE_STATUSES and _is_today_task(t, start, end)
        ]
        aggs = await self._activity.daily_aggregates([employee.id], start, end)
        ocr = await self._screenshots.ocr_text_for_day(employee.id, start, end)
        ocr_text = "\n".join(ocr)[: self._settings.eod_ocr_char_budget]

        lines = [
            f"Employee: {employee.full_name}"
            + (f" ({employee.job_title})" if employee.job_title else ""),
            f"Department: {employee.department or 'n/a'}",
            f"Time worked today: {_worked_summary(aggs.get(employee.id))}",
            f"Tasks completed today: {_join(completed)}",
            f"Tasks in progress / due today: {_join(active)}",
            "",
            "Screen activity (OCR text from screenshots, may be noisy):",
            ocr_text or "(none captured)",
        ]
        return "\n".join(lines)

    async def local_hour(self, now: datetime) -> int:
        """Current hour in the org's attendance-policy timezone (scheduler gate)."""
        spec = await self._policy.spec()
        return now.astimezone(ZoneInfo(spec.timezone)).hour

    async def _local_date(self, now: datetime) -> str:
        spec = await self._policy.spec()
        return now.astimezone(ZoneInfo(spec.timezone)).date().isoformat()

    async def _day_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        spec = await self._policy.spec()
        tz = ZoneInfo(spec.timezone)
        local_date = now.astimezone(tz).date()
        start = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(UTC)
        return start, start + timedelta(days=1)


def _in_range(when: datetime | None, start: datetime, end: datetime) -> bool:
    return when is not None and start <= when < end


def _is_today_task(task: Task, start: datetime, end: datetime) -> bool:
    if task.status is TaskStatus.IN_PROGRESS:
        return True
    return _in_range(task.due_date, start, end) or _in_range(task.start_date, start, end)


def _join(items: list[str]) -> str:
    return "; ".join(items) if items else "none"


def _worked_summary(agg: DailyAgg | None) -> str:
    if agg is None:
        return "no activity captured"
    worked = max(0, int((agg.logout_at - agg.login_at).total_seconds() // 60))
    idle = min(worked, agg.idle_seconds // 60)
    active = max(0, worked - idle)
    return f"{worked} min on machine, ~{active} min active"
