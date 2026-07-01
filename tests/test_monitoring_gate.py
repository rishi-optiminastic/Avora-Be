"""Stop monitoring after a deliberate checkout.

Server-side privacy gate: once an employee deliberately clocks out (dashboard /
auto-checkout) for the day, their activity ingest is dropped (stored = nothing)
until they clock back in. A biometric out-punch (maybe lunch) does NOT suppress,
and an employee with no checkout at all (WFH / agent-only) is never suppressed.
The agent is not told — ingest still returns 202; we simply store nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivitySample
from app.models.work_session import WorkSession
from tests.conftest import _Seed, agent_headers


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


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


async def test_dropped_after_deliberate_checkout(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    await _add_session(db, seed, source="dashboard", clock_out_source="dashboard")
    assert await _ingest(client, seed) == 202  # agent unaware — not rejected
    assert await _stored_count(db, seed) == 0  # ...but nothing stored


async def test_dropped_after_auto_checkout(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    await _add_session(db, seed, source="dashboard", clock_out_source="auto")
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 0


async def test_stored_when_no_session(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # WFH / agent-only employee who never formally checks in is never suppressed.
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 1


async def test_biometric_out_does_not_suppress(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # A biometric out-punch may just be lunch — it must NOT stop monitoring.
    await _add_session(db, seed, source="biometric", clock_out_source="biometric")
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 1


async def test_resumes_after_clock_back_in(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # Deliberate checkout earlier, then clocked back in (open session) → monitored.
    await _add_session(db, seed, source="dashboard", clock_out_source="dashboard")
    await _add_session(db, seed, source="biometric", clock_out_source=None, open_session=True)
    assert await _ingest(client, seed) == 202
    assert await _stored_count(db, seed) == 1
