"""What the admin fleet is told about a device that is alive but producing nothing.

The bug this pins: a suppressed upload still marks the device alive
(`ActivityService._advance_device` advances the sequence and touches
`last_seen_at` so replay protection holds), while the sample and every
screenshot are dropped. A reader with only `last_seen_at` therefore sees a
freshly-seen device and calls it healthy - and the admin goes looking for
screenshots that were never stored, for somebody who never shows up online.

`DeviceRead.capture_window` is the server saying which of the two it is, and why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import generate_device_token, hash_device_token
from app.models.activity import ActivitySample
from app.models.attendance_policy import AttendancePolicy
from app.models.device import Device
from app.models.work_session import WorkSession
from tests.conftest import _Seed, agent_headers, auth_headers


async def _set_working_days(db: AsyncSession, working_days_per_week: int) -> None:
    """Pin the week so the day-of-week gate does not depend on when the suite runs."""
    db.add(AttendancePolicy(working_days_per_week=working_days_per_week))
    await db.commit()


async def _clock_in(db: AsyncSession, employee_id: object) -> None:
    db.add(
        WorkSession(
            employee_id=employee_id,
            clock_in_at=datetime.now(UTC) - timedelta(hours=1),
            source="dashboard",
        )
    )
    await db.commit()


async def _ingest(client: AsyncClient, seed: _Seed, sequence: int = 1) -> int:
    """One activity sample from the seeded agent, exactly as the laptop sends it."""
    raw, headers = agent_headers(
        seed.device_raw_token,
        {
            "sequence": sequence,
            "client_timestamp": datetime.now(UTC).isoformat(),
            "active_window": "Code.app",
            "idle_seconds": 5,
        },
    )
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    return resp.status_code


async def _stored(db: AsyncSession, employee_id: object) -> int:
    total = await db.scalar(
        select(func.count())
        .select_from(ActivitySample)
        .where(ActivitySample.employee_id == employee_id)
    )
    return int(total or 0)


async def _devices(client: AsyncClient, settings: Settings, seed: _Seed) -> list[dict[str, object]]:
    resp = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, object]] = resp.json()
    return rows


def _for(rows: list[dict[str, object]], employee_id: object) -> dict[str, object]:
    matches = [r for r in rows if r["employee_id"] == str(employee_id)]
    assert len(matches) == 1, f"expected one device row, got {len(matches)}"
    return matches[0]


async def test_live_agent_with_nothing_stored_is_not_called_capturing(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The regression. Agent talking to us, no open session, nothing stored."""
    await _set_working_days(db, 7)
    assert await _ingest(client, seed) == 202  # accepted, and dropped

    assert await _stored(db, seed.report.id) == 0  # the sample went nowhere

    row = _for(await _devices(client, settings, seed), seed.report.id)
    # The agent IS alive - this is what made the old badge read "Reporting"...
    assert row["last_seen_at"] is not None
    # ...while nothing it sent was kept, which is now said out loud.
    assert row["capture_window"] == "not_clocked_in"


async def test_open_session_on_a_working_day_is_capturing(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _set_working_days(db, 7)
    await _clock_in(db, seed.report.id)
    assert await _ingest(client, seed) == 202

    assert await _stored(db, seed.report.id) == 1  # and this one was kept

    row = _for(await _devices(client, settings, seed), seed.report.id)
    assert row["capture_window"] == "capturing"


async def test_a_non_working_day_is_named_rather_than_blamed_on_the_person(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """No working days configured (stands in for a Sunday): even an open session
    stores nothing, and nobody should be chased for it."""
    await _set_working_days(db, 0)
    await _clock_in(db, seed.report.id)

    row = _for(await _devices(client, settings, seed), seed.report.id)
    assert row["capture_window"] == "non_working_day"


async def test_each_device_carries_its_own_owner_verdict(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The batch lookup must not smear one person's state across the roster."""
    await _set_working_days(db, 7)
    await _clock_in(db, seed.report.id)  # clocked in
    raw_token = generate_device_token()
    db.add(
        Device(
            employee_id=seed.manager.id,  # not clocked in
            label="manager-mbp",
            token_hash=hash_device_token(settings, raw_token),
        )
    )
    await db.commit()

    rows = await _devices(client, settings, seed)
    assert _for(rows, seed.report.id)["capture_window"] == "capturing"
    assert _for(rows, seed.manager.id)["capture_window"] == "not_clocked_in"


async def test_revoke_response_also_carries_the_state(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """`to_read` feeds the single-device responses the UI swaps into its list;
    leaving it blank there would put a stale row back on the page."""
    await _set_working_days(db, 7)
    resp = await client.post(
        f"/api/v1/devices/{seed.device.id}/revoke",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["capture_window"] == "not_clocked_in"


async def test_the_state_leaks_to_nobody_out_of_scope(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The field rides along with an already-scoped read and widens nothing: an
    outsider learns neither the device nor whether its owner is at work."""
    await _set_working_days(db, 7)
    await _clock_in(db, seed.report.id)
    resp = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 200
    assert resp.json() == []
