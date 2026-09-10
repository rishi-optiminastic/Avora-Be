"""Auto-checkout - forgotten open sessions close only after the trigger time AND
a buffer past the office window. Silence from the agent is never treated as proof
that someone went home: a session is cut short only when the machine goes quiet
AFTER the window closed. No HTTP surface; this is a worker service tested directly
with a controlled `now`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.activity import ActivitySample
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


async def _seen_at(db: AsyncSession, seed: _Seed, when: datetime, *, sequence: int = 1) -> None:
    """Record one activity sample, i.e. "the PC was alive at `when`"."""
    db.add(
        ActivitySample(
            device_id=seed.device.id,
            employee_id=seed.report.id,
            sequence=sequence,
            client_timestamp=when.astimezone(UTC),
            received_at=when.astimezone(UTC),
        )
    )
    await db.commit()


# The seeded org works 9 AM to 6 PM, so the trigger is 6 PM + the 2h buffer = 8 PM.
_AFTER_TRIGGER = (20, 30)


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


async def test_closed_when_the_pc_went_quiet_after_hours(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The one case we can actually prove: worked past the window, then shut down."""
    session = await _open_session_today(db, seed)
    shutdown = _ist_today(18, 40)  # 40 min after the 6 PM window closed
    await _seen_at(db, seed, shutdown)
    service = _build_service(db, settings)

    closed = await service.run_due(_ist_today(*_AFTER_TRIGGER))

    assert closed == 1
    refreshed = (
        await db.execute(select(WorkSession).where(WorkSession.id == session.id))
    ).scalar_one()
    assert refreshed.clock_out_at is not None
    assert refreshed.clock_out_source == "auto"
    # Stamped when the machine actually went quiet, so the extra 40 min still counts.
    assert refreshed.clock_out_at.astimezone(IST).hour == 18


async def test_today_is_never_closed_without_any_activity_signal(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """The bug this replaces: no agent data meant "closed at the window end", so
    anyone clocked in by biometric or the dashboard - with no desktop agent
    reporting at all - was cut off mid-shift while still at their desk."""
    session = await _open_session_today(db, seed)
    service = _build_service(db, settings)

    closed = await service.run_due(_ist_today(*_AFTER_TRIGGER))

    assert closed == 0
    await db.refresh(session)
    assert session.clock_out_at is None


async def test_today_is_never_closed_when_the_agent_died_mid_shift(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """A dead agent looks exactly like a switched-off machine. One real session was
    cut from a full day to 40 minutes this way, so mid-day silence closes nothing."""
    session = await _open_session_today(db, seed)
    await _seen_at(db, seed, _ist_today(9, 31))  # last ping, hours before the window ends
    service = _build_service(db, settings)

    closed = await service.run_due(_ist_today(*_AFTER_TRIGGER))

    assert closed == 0
    await db.refresh(session)
    assert session.clock_out_at is None


async def test_still_active_is_left_alone(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    session = await _open_session_today(db, seed)
    now = _ist_today(*_AFTER_TRIGGER)
    await _seen_at(db, seed, now)  # pinged just now: they are sitting there working
    service = _build_service(db, settings)

    assert await service.run_due(now) == 0
    await db.refresh(session)
    assert session.clock_out_at is None


async def test_prior_day_with_a_dead_agent_falls_back_to_the_work_end(
    db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    """Yesterday's forgotten session must close, but on the scheduled work-end
    rather than a dead agent's last ping - otherwise the day is silently docked."""
    yesterday = datetime.now(IST).date() - timedelta(days=1)
    clock_in = datetime(yesterday.year, yesterday.month, yesterday.day, 9, 0, tzinfo=IST)
    session = WorkSession(
        employee_id=seed.report.id, clock_in_at=clock_in.astimezone(UTC), source="biometric"
    )
    db.add(session)
    await db.commit()
    await _seen_at(db, seed, clock_in.replace(hour=9, minute=31))
    service = _build_service(db, settings)

    closed = await service.run_due(_ist_today(*_AFTER_TRIGGER))

    assert closed == 1
    refreshed = (
        await db.execute(select(WorkSession).where(WorkSession.id == session.id))
    ).scalar_one()
    assert refreshed.clock_out_at is not None
    # 6 PM work-end, not the 9:31 AM ping.
    assert refreshed.clock_out_at.astimezone(IST).hour == 18
