"""End-of-Day report business rules.

Generate (system/admin) → employee reviews/edits → approve → email manager+admins,
with a cutoff auto-send for unreviewed drafts. Absent employees get no report.
Reads are scoped via the employee scope (`can_read`); edit/approve are author-only.
No FastAPI objects here (Layering §4); the screenshot signal is OCR *text* only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.eod_report import EodReport, EodStatus
from app.models.notification import NotificationKind
from app.models.task import Task, TaskStatus
from app.repositories.activity import ActivityRepository, DailyAgg, idle_minutes
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
        employee = await self._employees.get(report.employee_id)
        recipients = await self._recipients(employee) if employee is not None else []
        await self._send(report, employee, recipients)
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
        Returns the number of drafts created. Idempotent per (employee, date).

        Done in three phases so the slow part (the LLM) runs concurrently while
        all DB access stays sequential on the one shared session:
          1. gather each person's signals (DB),
          2. summarise them concurrently (network — bounded fan-out),
          3. persist the drafts (DB)."""
        if caller.role is not Role.ADMIN:
            raise AuthorizationError()
        report_date = await self._local_date(now)
        start, end = await self._day_bounds(now)
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        already = {r.employee_id for r in await self._reports.list_for_employees(ids, report_date)}
        attendance = {a.employee_id: a.status for a in await self._attendance.daily(caller, now)}

        # Partition: absent/unknown → record a skip; present + ungenerated → queue.
        present: list[Employee] = []
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
            else:
                present.append(employee)
        if not present:
            return 0

        # Phase 1 — build each person's context (DB; sequential on the shared session).
        contexts = [await self._build_context(caller, e, start, end) for e in present]
        # Phase 2 — summarise concurrently; the LLM is network-bound, so bound the fan-out.
        drafts = await self._summarise_all(contexts)
        # Phase 3 — persist (DB; sequential).
        send_label = await self._send_time_label()
        created = 0
        for employee, draft in zip(present, drafts, strict=True):
            created += await self._persist_draft(employee, report_date, draft, send_label)
        return created

    async def _summarise_all(self, contexts: list[str]) -> list[EodDraftContent | None]:
        """Run the per-employee LLM calls concurrently, capped at `eod_concurrency`
        so we don't hammer the provider. A failed call yields None (→ FAILED row)."""
        limit = asyncio.Semaphore(self._settings.eod_concurrency)

        async def summarise(context: str) -> EodDraftContent | None:
            async with limit:
                try:
                    return await self._llm.generate_eod(context)
                except LlmError as exc:
                    logger.warning("eod generation failed: %s", exc)
                    return None

        return await asyncio.gather(*(summarise(c) for c in contexts))

    async def _persist_draft(
        self,
        employee: Employee,
        report_date: str,
        draft: EodDraftContent | None,
        send_label: str,
    ) -> int:
        if draft is None:
            await self._reports.create(
                employee_id=employee.id,
                report_date=report_date,
                status=EodStatus.FAILED,
                error="llm generation failed",
                model=self._settings.eod_model,
            )
            return 0
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
            title="Your End-of-Day report is drafted",
            body=(
                "Review and edit it if you want — it'll be sent to your manager "
                f"automatically at {send_label}."
            ),
            link=_EOD_LINK,
            entity_type="eod_report",
            entity_id=report.id,
        )
        return 1

    async def run_due(self, now: datetime) -> tuple[int, int, int, int]:
        """One scheduler tick (local-tz gated): generate drafts once we're past the
        DRAFT time (16:30), auto-send drafts to manager+admins once past the SEND
        time (18:00), and once a day prune old monitoring data. Returns
        (drafts_created, drafts_sent, activity_rows_purged, screenshots_purged).
        Shared by the worker loop and the free-tier cron endpoint."""
        local_dt = await self._local_dt(now)
        minutes = local_dt.hour * 60 + local_dt.minute
        generated = 0
        draft_at = self._settings.eod_report_hour * 60 + self._settings.eod_report_minute
        if minutes >= draft_at:
            generated = await self.generate_for_day(SYSTEM_CALLER, now)
        sent = await self.auto_send_due(now, local_dt)
        purged_activity = purged_shots = 0
        if local_dt.hour == self._settings.activity_purge_hour:
            purged_activity = await self.purge_old_activity(now)
            purged_shots = await self.purge_old_screenshots(now)
        return generated, sent, purged_activity, purged_shots

    async def purge_old_activity(self, now: datetime) -> int:
        """Retention: drop activity samples older than the configured window.
        Only monitoring activity is pruned — nothing else is deleted."""
        cutoff = now - timedelta(days=self._settings.activity_retention_days)
        return await self._activity.purge_before(cutoff)

    async def purge_old_screenshots(self, now: datetime) -> int:
        """Retention: drop screenshots (and their S3 blobs) older than the window.
        Mirrors ScreenshotService.purge_old — blobs go before rows so nothing
        orphans. Only screenshots are pruned; nothing else is deleted."""
        cutoff = now - timedelta(days=self._settings.screenshot_retention_days)
        if self._settings.s3_enabled:
            await storage.delete_objects(await self._screenshots.object_keys_before(cutoff))
        return await self._screenshots.purge_before(cutoff)

    async def auto_send_due(self, now: datetime, local_dt: datetime) -> int:
        """Send drafts that are due to managers+admins, as-is. Returns count sent.

        Today's drafts are sent only once the local SEND time (18:00) has passed —
        that 16:30→18:00 gap is the employee's review window. Drafts from earlier
        days are always overdue and sent regardless of the current time.

        Recipients are resolved in bulk up front (admins once, all report owners +
        their managers in two batched lookups) — no per-report DB calls."""
        minutes = local_dt.hour * 60 + local_dt.minute
        send_at = self._settings.eod_send_hour * 60 + self._settings.eod_send_minute
        today = local_dt.date()
        # Past the send time → include today; otherwise only earlier days.
        through = today if minutes >= send_at else today - timedelta(days=1)
        overdue = await self._reports.list_drafts_through(through.isoformat())
        if not overdue:
            return 0
        admins = await self._employees.list_by_role(Role.ADMIN)
        owners = await self._employees.get_many([r.employee_id for r in overdue])
        managers = await self._employees.get_many(
            [e.manager_id for e in owners.values() if e.manager_id is not None]
        )
        sent = 0
        for report in overdue:
            report.status = EodStatus.APPROVED
            report.approved_at = now
            employee = owners.get(report.employee_id)
            recipients = (
                self._recipients_for(employee, managers.get(employee.manager_id), admins)
                if employee is not None and employee.manager_id is not None
                else self._recipients_for(employee, None, admins)
                if employee is not None
                else []
            )
            await self._send(report, employee, recipients)
            sent += 1
        return sent

    # ---- send -------------------------------------------------------------- #
    async def _send(
        self, report: EodReport, employee: Employee | None, recipients: list[str]
    ) -> None:
        if employee is not None and recipients:
            subject, html = eod_report_email(
                employee_name=employee.full_name,
                date_label=report.report_date,
                summary=report.edited_summary or report.summary,
                highlights=EodDraftContent.model_validate(report.highlights or {}),
            )
            for recipient in recipients:
                await self._email.send(to=recipient, subject=subject, html=html)
        report.status = EodStatus.SENT  # employee gone or no recipients → close it out
        report.sent_at = datetime.now(UTC)
        await self._reports.flush()
        await self._audit.append(
            actor="system:eod",
            action="eod.send",
            target=f"eod:{report.id}",
        )

    async def _recipients(self, employee: Employee) -> list[str]:
        """Single-report path (the `approve()` endpoint) — one manager + admins."""
        admins = await self._employees.list_by_role(Role.ADMIN)
        manager = (
            await self._employees.get(employee.manager_id)
            if employee.manager_id is not None
            else None
        )
        return self._recipients_for(employee, manager, admins)

    @staticmethod
    def _recipients_for(
        employee: Employee | None, manager: Employee | None, admins: Sequence[Employee]
    ) -> list[str]:
        """Manager (if active) + admins, de-duped, minus the employee's own address."""
        if employee is None:
            return []
        emails: list[str] = []
        if manager is not None and manager.is_active:
            emails.append(manager.work_email)
        emails.extend(admin.work_email for admin in admins)
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

    async def _local_dt(self, now: datetime) -> datetime:
        """`now` in the org's attendance-policy timezone (the scheduler clock)."""
        spec = await self._policy.spec()
        return now.astimezone(ZoneInfo(spec.timezone))

    async def _send_time_label(self) -> str:
        """Human label for the auto-send time, e.g. '6:00 PM IST' — shown to the
        employee so they know their review deadline."""
        spec = await self._policy.spec()
        tz = ZoneInfo(spec.timezone)
        when = datetime(
            2000, 1, 1, self._settings.eod_send_hour, self._settings.eod_send_minute, tzinfo=tz
        )
        return when.strftime("%-I:%M %p %Z")

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
    active = max(0, worked - idle_minutes(agg, worked))
    return f"{worked} min on machine, ~{active} min active"
