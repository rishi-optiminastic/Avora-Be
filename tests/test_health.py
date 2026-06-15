from __future__ import annotations

from httpx import AsyncClient


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_pings_db(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
