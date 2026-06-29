"""Auto-checkout — forgotten open sessions close only after the trigger time
(5 PM local), independent of the attendance work-end. No HTTP surface; this is a
worker service tested directly with a controlled `now`."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.work_session import WorkSession
from app.repositories.activity import ActivityRepository
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.auto_checkout_service import AutoCheckoutService
from tests.conftest import _Seed

IST = ZoneInfo("Asia/Kolkata")


class _SilentEmail:
    async def send_forgot_checkout(
        self, *, to: str, employee_name: str, day_label: str, checkout_label: str
    ) -> None:
        return None


def _build_service(db: AsyncSession, settings: Settings) -> AutoCheckoutService:
    audit = AuditRepository(db)
    return AutoCheckoutService(
        WorkSessionRepository(db),
        ActivityRepository(db),
        EmployeeRepository(db),
        AttendancePolicyService(AttendancePolicyRepository(db), audit),
        _SilentEmail(),  # type: ignore[arg-type]
        audit,
        settings,
    )


async def _open_session_today(db: AsyncSession, seed: _Seed) -> WorkSession:
    today = datetime.now(IST).date()
    clock_in = datetime(today.year, today.month, today.day, 9, 0, tzinfo=IST).astimezone(UTC)
    session = WorkSession(employee_id=seed.report.id, clock_in_at=clock_in, source="dashboard")
    db.add(session)
    await db.commit()
    return session


def _ist_today(hour: int, minute: int) -> datetime:
    today = datetime.now(IST).date()
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=IST)


async def test_not_closed_before_trigger_time(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    session = await _open_session_today(db, seed)
    service = _build_service(db, settings)

    # 4:00 PM IST — before the 5 PM trigger, even though it's a forgotten session.
    closed = await service.run_due(_ist_today(16, 0))
    assert closed == 0
    await db.refresh(session)
    assert session.clock_out_at is None


async def test_closed_after_trigger_time(db: AsyncSession, seed: _Seed, settings: Settings) -> None:
    session = await _open_session_today(db, seed)
    service = _build_service(db, settings)

    # 5:30 PM IST — past the trigger (and the org work-end is 6 PM, proving the
    # trigger, not the work-end, is what gates it).
    closed = await service.run_due(_ist_today(17, 30))
    assert closed == 1
    refreshed = (
        await db.execute(select(WorkSession).where(WorkSession.id == session.id))
    ).scalar_one()
    assert refreshed.clock_out_at is not None
    assert refreshed.clock_out_source == "auto"
