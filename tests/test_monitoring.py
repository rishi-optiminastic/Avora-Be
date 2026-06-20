"""Attendance + live-activity reads: scoping and derivation from raw samples."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, agent_headers, auth_headers


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


async def _ingest(client: AsyncClient, seed: _Seed, sequence: int) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _sample(sequence))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_attendance_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/attendance")).status_code == 401


async def test_attendance_marks_present_and_is_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _ingest(client, seed, 1)
    resp = await client.get("/api/v1/attendance", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    rows = {r["employee_id"]: r for r in resp.json()}
    # Manager sees themselves + their report only.
    assert set(rows) == {str(seed.manager.id), str(seed.report.id)}
    report = rows[str(seed.report.id)]
    assert report["status"] in ("full_day", "half_day", "late")
    assert report["login_at"] is not None
    assert rows[str(seed.manager.id)]["status"] == "absent"  # no samples


async def test_activity_now_shows_online(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _ingest(client, seed, 1)
    resp = await client.get("/api/v1/activity/now", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    rows = {r["employee_id"]: r for r in resp.json()}
    assert rows[str(seed.report.id)]["online"] is True
    assert rows[str(seed.report.id)]["active_window"] == "Code.app"


async def test_timeline_is_scoped(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await _ingest(client, seed, 1)
    mine = await client.get(
        f"/api/v1/activity/{seed.report.id}", headers=auth_headers(settings, seed.manager)
    )
    assert mine.status_code == 200
    assert len(mine.json()) >= 1

    # An out-of-scope caller gets 404 (never leaks existence).
    blocked = await client.get(
        f"/api/v1/activity/{seed.report.id}", headers=auth_headers(settings, seed.outsider)
    )
    assert blocked.status_code == 404
