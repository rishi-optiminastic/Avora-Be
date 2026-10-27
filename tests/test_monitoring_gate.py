"""Monitoring capture window (server-side privacy gate).

We store activity/screenshots ONLY during an open work session on a working day.
Outside that window nothing is stored: before check-in, after checkout (by any
source — dashboard, auto, or biometric), and on non-working days (e.g. Sunday).
The agent is not told to stop — ingest still returns 202; we simply store nothing.

Tests pin `working_days_per_week` so the day-of-week gate is deterministic
regardless of when the suite runs: 7 ⇒ every day is a working day (isolate the
session gate); 0 ⇒ no working days (isolate the day gate).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivitySample
from app.models.attendance_policy import AttendancePolicy
from app.models.work_session import WorkSession
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.monitoring import CaptureWindowState
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.monitoring_gate import MonitoringGateService
from tests.conftest import _Seed, agent_headers


def _gate(db: AsyncSession) -> MonitoringGateService:
    return MonitoringGateService(
        WorkSessionRepository(db),
        AttendancePolicyService(AttendancePolicyRepository(db), AuditRepository(db)),
    )


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


async def _set_working_days(db: AsyncSession, working_days_per_week: int) -> None:
    """Pin the org working-days-per-week so the day-of-week gate is deterministic."""
    db.add(AttendancePolicy(working_days_per_week=working_days_per_week))
    await db.commit()


async def _add_session(
    db: AsyncSession,
    seed: _Seed,
    *,
    source: str,
    clock_out_source: str | None,
    open_session: bool = False,
) -> None:
    now = datetime.now(UTC)
    db.add(
        WorkSession(
            employee_id=seed.report.id,
            clock_in_at=now - timedelta(hours=8),
            clock_out_at=None if open_session else now,
            source=source,
            clock_out_source=None if open_session else clock_out_source,
        )
    )
    await db.commit()


async def _stored_count(db: AsyncSession, seed: _Seed) -> int:
    total = await db.scalar(
        select(func.count())
        .select_from(ActivitySample)
        .where(ActivitySample.employee_id == seed.report.id)
    )
    return int(total or 0)


async def _ingest(client: AsyncClient, seed: _Seed) -> int:
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    return resp.status_code


async def test_stored_during_open_session(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # Checked in (open session) on a working day → captured.
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="biometric", clock_out_source=None, open_session=True)
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 1


async def test_dropped_before_checkin(client: AsyncClient, db: AsyncSession, seed: _Seed) -> None:
    # No session yet today → before check-in → nothing stored.
    await _set_working_days(db, 7)
    assert await _ingest(client, seed) == 202  # agent unaware — not rejected
    assert await _stored_count(db, seed) == 0


async def test_dropped_after_dashboard_checkout(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="dashboard", clock_out_source="dashboard")
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 0


async def test_dropped_after_biometric_checkout(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # A closed biometric session (out-punch) now also stops capture — no open
    # session means checked out.
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="biometric", clock_out_source="biometric")
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 0


async def test_resumes_after_clock_back_in(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # Checked out earlier, then clocked back in (open session) → captured again.
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="dashboard", clock_out_source="dashboard")
    await _add_session(db, seed, source="biometric", clock_out_source=None, open_session=True)
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 1


async def test_dropped_on_non_working_day(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # No working days configured → even an open session is not captured (proves the
    # day-of-week gate, e.g. a Sunday, drops regardless of check-in state).
    await _set_working_days(db, 0)
    await _add_session(db, seed, source="biometric", clock_out_source=None, open_session=True)
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 0


# --------------------------------------------------------------------------- #
# The same verdict, with its reason kept. `should_suppress` flattens these to a
# bool for the ingest path; the admin fleet needs to know WHICH of them it is,
# because "they have not clocked in" is somebody's to fix and "it is Sunday" is
# nobody's.
# --------------------------------------------------------------------------- #


async def test_state_says_not_clocked_in_rather_than_just_no(db: AsyncSession, seed: _Seed) -> None:
    await _set_working_days(db, 7)
    gate = _gate(db)
    assert await gate.state_for(seed.report.id) is CaptureWindowState.NOT_CLOCKED_IN
    assert await gate.should_suppress(seed.report.id) is True


async def test_state_says_capturing_during_an_open_session(db: AsyncSession, seed: _Seed) -> None:
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="dashboard", clock_out_source=None, open_session=True)
    gate = _gate(db)
    assert await gate.state_for(seed.report.id) is CaptureWindowState.CAPTURING
    assert await gate.should_suppress(seed.report.id) is False


async def test_state_blames_the_day_not_the_person(db: AsyncSession, seed: _Seed) -> None:
    # An open session on a non-working day: still nothing stored, but the reason
    # is the calendar - chasing the employee would be wrong.
    await _set_working_days(db, 0)
    await _add_session(db, seed, source="dashboard", clock_out_source=None, open_session=True)
    assert await _gate(db).state_for(seed.report.id) is CaptureWindowState.NON_WORKING_DAY


async def test_states_for_resolves_a_mixed_roster(db: AsyncSession, seed: _Seed) -> None:
    await _set_working_days(db, 7)
    await _add_session(db, seed, source="dashboard", clock_out_source=None, open_session=True)
    states = await _gate(db).states_for([seed.report.id, seed.manager.id, seed.outsider.id])
    assert states == {
        seed.report.id: CaptureWindowState.CAPTURING,
        seed.manager.id: CaptureWindowState.NOT_CLOCKED_IN,
        seed.outsider.id: CaptureWindowState.NOT_CLOCKED_IN,
    }


async def test_states_for_collapses_duplicates(db: AsyncSession, seed: _Seed) -> None:
    # Two laptops, one person: the roster the fleet view hands over has the id
    # twice, and the answer is keyed by person.
    await _set_working_days(db, 7)
    states = await _gate(db).states_for([seed.report.id, seed.report.id])
    assert states == {seed.report.id: CaptureWindowState.NOT_CLOCKED_IN}


async def test_states_for_an_empty_roster_asks_nothing(db: AsyncSession) -> None:
    assert await _gate(db).states_for([]) == {}
