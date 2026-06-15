"""Agent ingest: HMAC, replay, and sequence-ordering tests (Security rule 5.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from tests.conftest import _Seed, agent_headers


def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 5,
    }


async def test_valid_sample_is_accepted(client: AsyncClient, seed: _Seed) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True


async def test_bad_hmac_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    headers["X-Signature"] = "sha256=deadbeef"
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_unknown_token_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    raw, headers = agent_headers("not-a-real-token", _sample(1))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_replayed_sequence_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    first = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert first.status_code == 202

    # Replaying the same sequence must be rejected (409).
    raw2, headers2 = agent_headers(seed.device_raw_token, _sample(1))
    replay = await client.post("/api/v1/activity/ingest", content=raw2, headers=headers2)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "replay_detected"


async def test_out_of_order_sequence_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _sample(5))
    assert (
        await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    ).status_code == 202

    # A lower sequence after a higher one is out of order -> rejected.
    raw2, headers2 = agent_headers(seed.device_raw_token, _sample(3))
    resp = await client.post("/api/v1/activity/ingest", content=raw2, headers=headers2)
    assert resp.status_code == 409


async def test_impossible_idle_value_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    bad = _sample(1)
    bad["idle_seconds"] = -10  # impossible
    raw, headers = agent_headers(seed.device_raw_token, bad)
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 422
