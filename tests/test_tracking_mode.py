"""Work/personal tracking mode: the self toggle + the ingest capture gate.

When an employee switches to PERSONAL mode, the server must drop everything the
agent sends (activity + screenshots) — that gate is the privacy guarantee, not
the agent's cooperation (the agent is hostile by assumption, rule 5.1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.config import Settings
from app.core.security import compute_hmac_sha256
from tests.conftest import _Seed, agent_headers, auth_headers


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


def _raw_agent_headers(raw_token: str, body: bytes, content_type: str) -> dict[str, str]:
    """Device headers for a raw (non-JSON) body — HMAC over the exact bytes."""
    return {
        "Authorization": f"Bearer {raw_token}",
        "X-Signature": compute_hmac_sha256(raw_token, body),
        "Content-Type": content_type,
    }


async def _set_mode(client: AsyncClient, settings: Settings, seed: _Seed, mode: str) -> None:
    resp = await client.patch(
        "/api/v1/employees/me/tracking-mode",
        json={"mode": mode},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tracking_mode"] == mode


async def test_toggle_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.patch("/api/v1/employees/me/tracking-mode", json={"mode": "personal"})
    assert resp.status_code == 401


async def test_employee_can_toggle_own_mode(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_mode(client, settings, seed, "personal")
    # /me reflects it.
    me = await client.get("/api/v1/employees/me", headers=auth_headers(settings, seed.report))
    assert me.json()["tracking_mode"] == "personal"
    # A mode_personal command is queued for the agent.
    inbox = await client.get(
        "/api/v1/pings/pending",
        headers=_raw_agent_headers(seed.device_raw_token, b"", "application/json"),
    )
    assert inbox.status_code == 200
    assert any(p["kind"] == "mode_personal" for p in inbox.json())


async def test_activity_dropped_in_personal_mode(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_mode(client, settings, seed, "personal")
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    paused = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert paused.status_code == 202
    assert paused.json()["accepted"] is False
    assert "tracking_paused" in paused.json()["flags"]

    # Back to work → the same sequence is now stored (it was never advanced).
    await _set_mode(client, settings, seed, "work")
    raw2, headers2 = agent_headers(seed.device_raw_token, _sample(1))
    ok = await client.post("/api/v1/activity/ingest", content=raw2, headers=headers2)
    assert ok.status_code == 202
    assert ok.json()["accepted"] is True


async def test_screenshot_dropped_in_personal_mode(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_mode(client, settings, seed, "personal")
    image = b"\xff\xd8\xff\xe0fake-jpeg"
    headers = _raw_agent_headers(seed.device_raw_token, image, "image/jpeg")
    resp = await client.post("/api/v1/screenshots", content=image, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["accepted"] is False
