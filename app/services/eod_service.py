"""End-of-Day report business rules.

Generate (system/admin) → employee reviews/edits → approve → email manager+admins,
with a cutoff auto-send for unreviewed drafts. Absent employees get no report.
Reads are scoped via the employee scope (`can_read`); edit/approve are author-only.
No FastAPI objects here (Layering §4); the screenshot signal is OCR *text* only.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.eod_report import EodReport, EodStatus
from app.models.notification import NotificationKind
from app.models.screenshot import Screenshot
from app.models.task import Task, TaskStatus
from app.repositories.activity import ActivityRepository, DailyAgg, idle_minutes
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.schemas.auth import CurrentUser
from app.schemas.eod import (
    EodCumulativeMember,
    EodCumulativeRead,
    EodCumulativeTotals,
    EodDraftContent,
    EodReportRead,
)
from app.schemas.monitoring import AttendanceStatus
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.email_templates import eod_report_email
from app.services.eod_settings_service import EodSchedule, EodSettingsService
from app.services.llm_service import LlmError, LlmService
from app.services.notification_service import NotificationService

logger = get_logger("app.eod")

# Non-human admin caller for the scheduler (whole active org; sentinel id is
# never matched on — admin's scope clause ignores employee_id). Mirrors payroll.
SYSTEM_CALLER = CurrentUser(employee_id=uuid.UUID(int=0), role=Role.ADMIN, manager_id=None)

_EOD_LINK = "/dashboard/me/eod"
_ACTIVE_STATUSES = (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)

# Per-person history default window (calendar days back from today).
_HISTORY_DEFAULT_SPAN = 30
# Reports that carry real content (vs. absent/failed placeholders) — the digest
# totals and content list are built from these only.
_CONTENT_STATUSES = (EodStatus.DRAFT, EodStatus.APPROVED, EodStatus.SENT)
# Coverage precedence when a member has several rows in the window: a submitted
# day beats a pending draft beats a failed run beats an absence.
_COVERAGE_RANK = {
    EodStatus.SENT: 4,
    EodStatus.APPROVED: 4,
    EodStatus.DRAFT: 3,
    EodStatus.FAILED: 2,
    EodStatus.SKIPPED_ABSENT: 1,
}


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
        eod_settings: EodSettingsService,
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
        self._eod_settings = eod_settings
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

    async def history_for_employee(
        self,
        caller: CurrentUser,
        employee_id: uuid.UUID | None,
        from_date: str | None,
        to_date: str | None,
        now: datetime,
    ) -> list[EodReportRead]:
        """A single person's past reports, newest first. Defaults to the caller;
        a manager/HR/admin may pass another `employee_id` within their scope. Out
        of scope → 404 (never reveal a report exists, §7)."""
        target = employee_id or caller.employee_id
        if not await self._employees.can_read(caller, target):
            raise NotFoundError()
        start, end = await self._resolve_range(now, from_date, to_date, _HISTORY_DEFAULT_SPAN)
        reports = await self._reports.list_for_employee_between(target, start, end)
        return [EodReportRead.from_model(r) for r in reports]

    async def cumulative_for_scope(
        self,
        caller: CurrentUser,
        from_date: str | None,
        to_date: str | None,
        now: datetime,
    ) -> EodCumulativeRead:
        """A rolled-up digest of the caller's team over a window (defaults to a
        single day): per-person coverage, summed effort, and every report with
        content. Scoped via the employee scope, so a manager sees only their
        reports, a senior manager their department, HR/admin the org."""
        start, end = await self._resolve_range(now, from_date, to_date, 0)
        employees = await self._employees.all_in_scope(caller)
        member_ids = [e.id for e in employees]
        reports = await self._reports.list_for_employees_between(member_ids, start, end)

        # Best (highest-precedence, then latest) row per member + summed totals.
        best: dict[uuid.UUID, EodReport] = {}
        totals = EodCumulativeTotals()
        content: list[EodReport] = []
        for report in reports:
            current = best.get(report.employee_id)
            if current is None or _outranks(report, current):
                best[report.employee_id] = report
            if report.status in _CONTENT_STATUSES:
                content.append(report)
                metrics = report.metrics or {}
                highlights = EodDraftContent.model_validate(report.highlights or {})
                totals.worked_minutes += int(metrics.get("worked_minutes", 0) or 0)
                totals.active_minutes += int(metrics.get("active_minutes", 0) or 0)
                totals.tasks_done += int(
                    metrics.get("tasks_done", len(highlights.tasks_completed)) or 0
                )
                totals.blockers += len(highlights.blockers)

        counts = {"submitted": 0, "pending": 0, "absent": 0, "failed": 0, "missing": 0}
        members: list[EodCumulativeMember] = []
        for employee in employees:
            row = best.get(employee.id)
            coverage = _coverage(row.status if row is not None else None)
            counts[coverage] += 1
            members.append(
                EodCumulativeMember(
                    employee_id=employee.id,
                    name=employee.full_name,
                    department=employee.department,
                    job_title=employee.job_title,
                    coverage=coverage,
                    latest_report_id=row.id if row is not None else None,
                    latest_report_date=row.report_date if row is not None else None,
                )
            )
        members.sort(key=lambda m: m.name.lower())

        name_of = {e.id: e.full_name for e in employees}
        content.sort(key=lambda r: (r.report_date, name_of.get(r.employee_id, "")), reverse=True)
        member_count = len(employees)
        rate = counts["submitted"] / member_count if member_count else 0.0
        return EodCumulativeRead(
            from_date=start,
            to_date=end,
            member_count=member_count,
            submitted=counts["submitted"],
            pending=counts["pending"],
            absent=counts["absent"],
            failed=counts["failed"],
            missing=counts["missing"],
            submission_rate=round(rate, 4),
            totals=totals,
            reports=[EodReportRead.from_model(r) for r in content],
            members=members,
        )

    async def _resolve_range(
        self, now: datetime, from_date: str | None, to_date: str | None, default_span_days: int
    ) -> tuple[str, str]:
        """Resolve a [start, end] window of local YYYY-MM-DD strings. `to` defaults
        to today; `from` defaults to `to` minus `default_span_days` (0 = single
        day). Malformed or reversed inputs are clamped, never trusted (§1)."""
        today = await self._local_date(now)
        end = _valid_date(to_date) or today
        if from_date:
            start = _valid_date(from_date) or end
        else:
            start = (
                date.fromisoformat(end) - timedelta(days=max(0, default_span_days))
            ).isoformat()
        if start > end:
            start, end = end, start
        return start, end

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

        # Phase 0 — vision (optional): analyse a few sampled screenshots per
        # employee into a screen-context block. DB access stays sequential; the
        # model/S3 calls fan out concurrently inside.
        vision_blocks = await self._vision_blocks(present, start, end)
        # Phase 1 — build each person's context (DB; sequential on the shared session).
        built = [
            await self._build_context(caller, e, start, end, vision_blocks.get(e.id, ""))
            for e in present
        ]
        # Phase 2 — summarise concurrently; the LLM is network-bound, so bound the
        # fan-out. An empty-signal day skips the model and gets a fixed draft.
        drafts = await self._summarise_all(
            [
                (ctx, has_signal, e.full_name)
                for e, (ctx, _, has_signal) in zip(present, built, strict=True)
            ]
        )
        # Phase 3 — persist (DB; sequential).
        send_label = await self._send_time_label()
        created = 0
        for employee, (_, metrics, _), draft in zip(present, built, drafts, strict=True):
            created += await self._persist_draft(employee, report_date, draft, send_label, metrics)
        return created

    async def _summarise_all(
        self, items: list[tuple[str, bool, str]]
    ) -> list[EodDraftContent | None]:
        """Run the per-employee LLM calls concurrently, capped at `eod_concurrency`
        so we don't hammer the provider. A day with no signal at all skips the model
        and gets a fixed empty-state draft; a failed call yields None (→ FAILED row)."""
        limit = asyncio.Semaphore(self._settings.eod_concurrency)

        async def summarise(item: tuple[str, bool, str]) -> EodDraftContent | None:
            context, has_signal, name = item
            if not has_signal:
                return _empty_draft(name)
            async with limit:
                try:
                    return await self._llm.generate_eod(context)
                except LlmError as exc:
                    logger.warning("eod generation failed: %s", exc)
                    return None

        return await asyncio.gather(*(summarise(i) for i in items))

    async def _persist_draft(
        self,
        employee: Employee,
        report_date: str,
        draft: EodDraftContent | None,
        send_label: str,
        metrics: dict[str, object],
    ) -> int:
        if draft is None:
            await self._reports.create(
                employee_id=employee.id,
                report_date=report_date,
                status=EodStatus.FAILED,
                metrics=metrics,
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
            metrics=metrics,
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
        """One scheduler tick (local-tz gated): when EOD is enabled, generate drafts
        once we're past the DRAFT time, auto-send drafts to manager+admins once past
        the SEND time; and once a day prune old monitoring data. The schedule + the
        on/off switch are read from the DB (`eod_settings`, editable in Settings), so
        changes take effect on the next tick with no redeploy. Returns
        (drafts_created, drafts_sent, activity_rows_purged, screenshots_purged).
        Shared by the worker loop and the free-tier cron endpoint."""
        local_dt = await self._local_dt(now)
        minutes = local_dt.hour * 60 + local_dt.minute
        sched = await self._eod_settings.spec()
        generated = sent = 0
        if sched.enabled:
            if minutes >= sched.draft_minutes:
                generated = await self.generate_for_day(SYSTEM_CALLER, now)
            sent = await self.auto_send_due(now, local_dt, sched)
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

    async def auto_send_due(
        self, now: datetime, local_dt: datetime, sched: EodSchedule | None = None
    ) -> int:
        """Send drafts that are due to managers+admins, as-is. Returns count sent.

        Hard window gate: EOD reports only ever leave at/after the local SEND time.
        Before it we send NOTHING — not even "overdue" prior-day drafts — so a draft
        left unsent (e.g. the worker was down across yesterday's window, or restarted
        mid-day) goes out at the NEXT send time, never at some arbitrary hour like
        15:40. At/after the send time we flush every pending draft through today
        (today's plus any earlier-day leftovers). The send time comes from the DB
        schedule (fetched here when not supplied by `run_due`).

        Recipients are resolved in bulk up front (admins once, all report owners +
        their managers in two batched lookups) — no per-report DB calls."""
        if sched is None:
            sched = await self._eod_settings.spec()
        minutes = local_dt.hour * 60 + local_dt.minute
        if minutes < sched.send_minutes:
            return 0
        overdue = await self._reports.list_drafts_through(local_dt.date().isoformat())
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
            highlights = EodDraftContent.model_validate(report.highlights or {})
            metrics = report.metrics or {}
            subject, html = eod_report_email(
                employee_name=employee.full_name,
                date_label=report.report_date,
                summary=report.edited_summary or report.summary,
                highlights=highlights,
                worked_minutes=int(metrics.get("worked_minutes", 0) or 0),
                active_minutes=int(metrics.get("active_minutes", 0) or 0),
                tasks_done=int(metrics.get("tasks_done", len(highlights.tasks_completed)) or 0),
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

    # ---- vision (sampled screenshot analysis) ----------------------------- #
    async def _vision_blocks(
        self, employees: list[Employee], start: datetime, end: datetime
    ) -> dict[uuid.UUID, str]:
        """Per-employee screen-context block from a few sampled screenshots (empty
        dict when vision is off). DB work — sampling the frames and caching results —
        stays sequential on the shared session; the model + S3 calls in between fan
        out concurrently. Cached `vision_json` is reused so a re-run never re-bills."""
        if not self._settings.eod_vision_active:
            return {}
        sampled: dict[uuid.UUID, list[Screenshot]] = {}
        for employee in employees:
            sampled[employee.id] = await self._screenshots.sample_for_day(
                employee.id, start, end, self._settings.eod_vision_sample_max
            )
        frames = [shot for shots in sampled.values() for shot in shots]
        if not frames:
            return {}
        limit = asyncio.Semaphore(self._settings.eod_concurrency)

        async def analyse(shot: Screenshot) -> tuple[uuid.UUID, dict[str, Any] | None]:
            if shot.vision_json:
                return shot.id, dict(shot.vision_json)
            data = await self._screenshot_bytes(shot)
            if data is None:
                return shot.id, None
            b64 = base64.b64encode(data).decode("ascii")
            async with limit:
                try:
                    result = await self._llm.describe_screen(
                        image_b64=b64, content_type=shot.content_type
                    )
                except LlmError as exc:
                    logger.warning("eod vision failed: %s", exc)
                    return shot.id, None
            return shot.id, result

        analysed = dict(await asyncio.gather(*(analyse(shot) for shot in frames)))
        dirty = False
        for shot in frames:
            result = analysed.get(shot.id)
            if result is not None and not shot.vision_json:
                shot.vision_json = result
                dirty = True
        if dirty:
            await self._screenshots.flush()
        return {
            emp_id: _format_vision([analysed.get(s.id) for s in shots])
            for emp_id, shots in sampled.items()
        }

    async def _screenshot_bytes(self, shot: Screenshot) -> bytes | None:
        """Image bytes for one sampled frame — the DB column (already loaded) or S3."""
        if shot.image is not None:
            return bytes(shot.image)
        if shot.object_key and self._settings.s3_enabled:
            try:
                return await storage.get_object(shot.object_key)
            except (ClientError, BotoCoreError):
                logger.warning("eod vision: s3 fetch failed")
                return None
        return None

    # ---- context + time helpers ------------------------------------------- #
    async def _build_context(
        self,
        caller: CurrentUser,
        employee: Employee,
        start: datetime,
        end: datetime,
        vision_block: str,
    ) -> tuple[str, dict[str, object], bool]:
        """Returns (LLM context text, day metrics for the email card, has-signal).
        `has_signal` is False only when nothing at all was observed — those days
        skip the model and render a fixed empty-state draft."""
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
        agg = aggs.get(employee.id)
        ocr = await self._screenshots.ocr_text_for_day(employee.id, start, end)
        ocr_text = "\n".join(ocr)[: self._settings.eod_ocr_char_budget]

        worked_minutes, active_minutes = _worked_metrics(agg)
        metrics: dict[str, object] = {
            "worked_minutes": worked_minutes,
            "active_minutes": active_minutes,
            "tasks_done": len(completed),
        }
        has_signal = bool(completed or active or agg or ocr_text or vision_block)

        lines = [
            f"Employee: {employee.full_name}"
            + (f" ({employee.job_title})" if employee.job_title else ""),
            f"Department: {employee.department or 'n/a'}",
            f"Time worked today: {_worked_summary(agg)}",
            f"Tasks completed today: {_join(completed)}",
            f"Tasks in progress / due today: {_join(active)}",
        ]
        if vision_block:
            lines += [
                "",
                "Screen context (from visual analysis of representative screenshots):",
                vision_block,
            ]
        lines += [
            "",
            "Screen activity (OCR text from screenshots, may be noisy):",
            ocr_text or "(none captured)",
        ]
        return "\n".join(lines), metrics, has_signal

    async def _local_dt(self, now: datetime) -> datetime:
        """`now` in the org's attendance-policy timezone (the scheduler clock)."""
        spec = await self._policy.spec()
        return now.astimezone(ZoneInfo(spec.timezone))

    async def _send_time_label(self) -> str:
        """Human label for the auto-send time, e.g. '6:00 PM IST' — shown to the
        employee so they know their review deadline. Read from the DB schedule."""
        spec = await self._policy.spec()
        sched = await self._eod_settings.spec()
        tz = ZoneInfo(spec.timezone)
        when = datetime(2000, 1, 1, sched.send_hour, sched.send_minute, tzinfo=tz)
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


def _outranks(candidate: EodReport, current: EodReport) -> bool:
    """True when `candidate` should replace `current` as a member's representative
    row: higher coverage precedence wins; ties break to the more recent date."""
    cand_rank = _COVERAGE_RANK.get(candidate.status, 0)
    cur_rank = _COVERAGE_RANK.get(current.status, 0)
    if cand_rank != cur_rank:
        return cand_rank > cur_rank
    return candidate.report_date > current.report_date


def _coverage(status: EodStatus | None) -> str:
    if status in (EodStatus.APPROVED, EodStatus.SENT):
        return "submitted"
    if status is EodStatus.DRAFT:
        return "pending"
    if status is EodStatus.FAILED:
        return "failed"
    if status is EodStatus.SKIPPED_ABSENT:
        return "absent"
    return "missing"


def _valid_date(value: str | None) -> str | None:
    """Normalise a client-supplied YYYY-MM-DD, or None if absent/malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _in_range(when: datetime | None, start: datetime, end: datetime) -> bool:
    return when is not None and start <= when < end


def _is_today_task(task: Task, start: datetime, end: datetime) -> bool:
    if task.status is TaskStatus.IN_PROGRESS:
        return True
    return _in_range(task.due_date, start, end) or _in_range(task.start_date, start, end)


def _join(items: list[str]) -> str:
    return "; ".join(items) if items else "none"


def _worked_metrics(agg: DailyAgg | None) -> tuple[int, int]:
    """(worked_minutes, active_minutes) from a day's rollup. Server-stamped times
    only; active = worked minus the idle share (see `idle_minutes`)."""
    if agg is None:
        return 0, 0
    worked = max(0, int((agg.logout_at - agg.login_at).total_seconds() // 60))
    active = max(0, worked - idle_minutes(agg, worked))
    return worked, active


def _worked_summary(agg: DailyAgg | None) -> str:
    if agg is None:
        return "no activity captured"
    worked, active = _worked_metrics(agg)
    return f"{worked} min on machine, ~{active} min active"


def _empty_draft(name: str) -> EodDraftContent:
    """Deterministic draft for a day with no signal at all — avoids spending an LLM
    call and keeps the wording consistent (vs. the model improvising each time)."""
    return EodDraftContent(
        summary=(
            f"No tracked activity was captured for {name} today — the Avora agent "
            "may not have been running, or the day was spent off the tracked device. "
            "No tasks were completed and no screen activity was recorded."
        ),
        confidence=0,
    )


def _format_vision(results: list[dict[str, Any] | None]) -> str:
    """Aggregate per-frame vision JSON into one compact context block: deduped apps
    and projects (insertion order preserved) plus per-frame observations. Empty when
    nothing was extracted (the EOD context then omits the visual section)."""
    apps: dict[str, None] = {}
    projects: dict[str, None] = {}
    observations: list[str] = []
    for result in results:
        if not result:
            continue
        for app in result.get("apps") or []:
            if isinstance(app, str) and app.strip():
                apps.setdefault(app.strip(), None)
        for project in result.get("projects") or []:
            if isinstance(project, str) and project.strip():
                projects.setdefault(project.strip(), None)
        working = (result.get("working_on") or "").strip()
        detail = (result.get("detail") or "").strip()
        if working:
            observations.append(f"{working} — {detail}" if detail else working)
    if not (apps or projects or observations):
        return ""
    parts: list[str] = []
    if apps:
        parts.append("Apps in use: " + ", ".join(apps))
    if projects:
        parts.append("Projects/areas on screen: " + ", ".join(projects))
    if observations:
        parts.append("Observed across the day:")
        parts.extend(f"- {observation}" for observation in observations[:12])
    return "\n".join(parts)
