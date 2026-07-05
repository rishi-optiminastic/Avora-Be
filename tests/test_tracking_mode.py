"""Capture is always on (work mode) — the personal-mode pause was removed.

The agent's activity is always stored now; the old per-employee work/personal
toggle (and its ingest capture gate) no longer exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from tests.conftest import _Seed, agent_headers, allow_capture, auth_headers


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


async def test_activity_captured_during_session(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    # Tracking mode no longer gates capture; during an open session on a working
    # day the sample is stored.
    await allow_capture(db, seed.report.id)
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True


async def test_tracking_mode_endpoint_removed(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The self toggle is gone — the path should no longer route.
    resp = await client.patch(
        "/api/v1/employees/me/tracking-mode",
        json={"mode": "personal"},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code in (404, 405)
