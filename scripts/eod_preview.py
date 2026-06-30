"""Preview EOD generation (dev / ops): for each employee today, print the exact
context fed to the LLM and the EOD draft it produces. Read-only — does NOT persist
drafts or send anything. Calls the real OpenRouter model (costs a few tokens).

    uv run python scripts/eod_preview.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.eod_report import EodReportRepository
from app.repositories.eod_settings import EodSettingsRepository
from app.repositories.notification import NotificationRepository
from app.repositories.regularization import RegularizationRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.attendance_service import AttendanceService
from app.services.email_service import EmailService
from app.services.eod_service import SYSTEM_CALLER, EodService
from app.services.eod_settings_service import EodSettingsService
from app.services.llm_service import LlmService
from app.services.notification_service import NotificationService


async def main() -> None:
    settings = get_settings()
    async with SessionFactory() as s:
        audit = AuditRepository(s)
        policy = AttendancePolicyService(AttendancePolicyRepository(s), audit)
        attendance = AttendanceService(
            EmployeeRepository(s),
            ActivityRepository(s),
            WorkSessionRepository(s),
            policy,
            RegularizationRepository(s),
        )
        eod = EodService(
            EodReportRepository(s),
            EmployeeRepository(s),
            TaskRepository(s),
            ActivityRepository(s),
            ScreenshotRepository(s),
            attendance,
            policy,
            LlmService(settings),
            EmailService(settings),
            NotificationService(NotificationRepository(s)),
            audit,
            EodSettingsService(EodSettingsRepository(s), audit, settings),
            settings,
        )

        now = datetime.now(UTC)
        start, end = await eod._day_bounds(now)
        statuses = {
            a.employee_id: a.status for a in await eod._attendance.daily(SYSTEM_CALLER, now)
        }
        employees = await EmployeeRepository(s).all_in_scope(SYSTEM_CALLER)

        # Vision is skipped in preview (keeps it cheap); pass an empty screen block.
        for e in employees:
            ctx, _metrics, _has_signal = await eod._build_context(
                SYSTEM_CALLER, e, start, end, ""
            )
            print("\n" + "=" * 78)
            print(f"{e.full_name}  ·  attendance={statuses.get(e.id)}")
            print("=" * 78)
            print("---- CONTEXT fed to the LLM ----")
            print(ctx.strip() or "(empty)")
            print("\n---- EOD DRAFT (model output) ----")
            try:
                draft = await eod._llm.generate_eod(ctx)
                print(draft.model_dump_json(indent=2))
            except Exception as exc:
                print(f"(LLM error: {exc})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
