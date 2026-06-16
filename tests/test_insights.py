"""Productivity insights: derivation (focus, at-risk, trend) and caller scoping."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, agent_headers, auth_headers


def _browse(sequence: int, url: str) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Google Chrome",
        "idle_seconds": 1,
        "url": url,
    }


async def _ingest(client: AsyncClient, seed: _Seed, sequence: int, url: str) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _browse(sequence, url))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_insights_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/insights")).status_code == 401


async def test_insights_flags_distraction_and_is_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # 1 productive + 3 distracting samples today ⇒ focus 25% ⇒ at risk.
    await _ingest(client, seed, 1, "https://github.com/optiminastic/avora")
    await _ingest(client, seed, 2, "https://www.youtube.com/watch?v=a")
    await _ingest(client, seed, 3, "https://www.instagram.com/x")
    await _ingest(client, seed, 4, "https://www.reddit.com/r/y")

    resp = await client.get("/api/v1/insights", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    rows = {r["employee_id"]: r for r in resp.json()}
    # Manager sees self + report only.
    assert set(rows) == {str(seed.manager.id), str(seed.report.id)}

    report = rows[str(seed.report.id)]
    assert report["focus_pct"] == 25
    assert report["distracting_minutes"] == 3
    assert report["at_risk"] is True
    assert len(report["trend"]) == 7
    assert report["trend"][-1] == 25  # today is the last trend point

    # The manager (no samples) isn't flagged.
    assert rows[str(seed.manager.id)]["at_risk"] is False
    assert rows[str(seed.manager.id)]["total_minutes"] == 0
