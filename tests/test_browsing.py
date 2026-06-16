"""Browsing reads: domain categorisation, derivation, and caller scoping."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.core.categories import ProductivityCategory, classify, extract_domain
from app.core.config import Settings
from tests.conftest import _Seed, agent_headers, auth_headers


def test_extract_domain_and_classify() -> None:
    assert extract_domain("https://www.GitHub.com/a/b") == "github.com"
    assert extract_domain("github.com/x") == "github.com"
    assert extract_domain(None) is None
    assert classify("github.com") is ProductivityCategory.PRODUCTIVE
    assert classify("gist.github.com") is ProductivityCategory.PRODUCTIVE  # subdomain suffix
    assert classify("youtube.com") is ProductivityCategory.DISTRACTING
    assert classify("example.com") is ProductivityCategory.NEUTRAL
    assert classify(None) is ProductivityCategory.NEUTRAL


def _browse_sample(sequence: int, url: str) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Google Chrome",
        "idle_seconds": 2,
        "url": url,
    }


async def _ingest_url(client: AsyncClient, seed: _Seed, sequence: int, url: str) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _browse_sample(sequence, url))
    resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_browsing_unauthenticated(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.get("/api/v1/browsing")).status_code == 401


async def test_browsing_categorises_and_is_scoped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The seed's device belongs to the report; one sample per domain ≈ one minute.
    await _ingest_url(client, seed, 1, "https://github.com/optiminastic/avora")
    await _ingest_url(client, seed, 2, "https://www.youtube.com/watch?v=demo")
    await _ingest_url(client, seed, 3, "https://example.com/page")

    resp = await client.get("/api/v1/browsing", headers=auth_headers(settings, seed.manager))
    assert resp.status_code == 200
    rows = {r["employee_id"]: r for r in resp.json()}
    # Manager sees themselves + their report only (scope), not the outsider.
    assert set(rows) == {str(seed.manager.id), str(seed.report.id)}
    assert str(seed.outsider.id) not in rows

    report = rows[str(seed.report.id)]
    assert report["total_minutes"] == 3
    assert report["productive_minutes"] == 1
    assert report["distracting_minutes"] == 1
    assert report["neutral_minutes"] == 1
    assert report["focus_pct"] == 33
    domains = {d["domain"]: d["category"] for d in report["top_domains"]}
    assert domains["github.com"] == "productive"
    assert domains["youtube.com"] == "distracting"
    assert domains["example.com"] == "neutral"
