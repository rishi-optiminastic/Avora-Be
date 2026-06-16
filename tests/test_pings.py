"""Ping authz: who may issue, scope enforcement, and the device inbox flow."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from app.core.security import compute_hmac_sha256
from tests.conftest import _Seed, auth_headers


def _empty_agent_headers(raw_token: str) -> dict[str, str]:
    # The inbox is a GET with an empty body; sign the empty body.
    return {
        "Authorization": f"Bearer {raw_token}",
        "X-Signature": compute_hmac_sha256(raw_token, b""),
    }


async def test_issue_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/pings", json={"employee_id": str(seed.report.id)})
    assert resp.status_code == 401


async def test_non_manager_cannot_ping(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/pings",
        json={"employee_id": str(seed.manager.id), "message": "hi"},
        headers=auth_headers(settings, seed.outsider),
    )
    assert resp.status_code in (403, 404)


async def test_manager_cannot_ping_out_of_scope(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Manager pinging the outsider (not a report) is 404 — never leaks existence.
    resp = await client.post(
        "/api/v1/pings",
        json={"employee_id": str(seed.outsider.id), "message": "hey"},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 404


async def test_manager_pings_report_and_agent_receives_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    issued = await client.post(
        "/api/v1/pings",
        json={"employee_id": str(seed.report.id), "message": "Standup in 5"},
        headers=auth_headers(settings, seed.manager),
    )
    assert issued.status_code == 201, issued.text
    assert issued.json()["message"] == "Standup in 5"

    # The report's agent polls its inbox and gets the ping…
    inbox = await client.get(
        "/api/v1/pings/pending", headers=_empty_agent_headers(seed.device_raw_token)
    )
    assert inbox.status_code == 200
    messages = [p["message"] for p in inbox.json()]
    assert "Standup in 5" in messages

    # …and a second poll is empty (delivered, not replayed).
    again = await client.get(
        "/api/v1/pings/pending", headers=_empty_agent_headers(seed.device_raw_token)
    )
    assert again.status_code == 200
    assert again.json() == []
