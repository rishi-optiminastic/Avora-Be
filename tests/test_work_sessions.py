"""Clock-in / clock-out (work sessions) — self-service + attendance integration."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers


async def test_clock_in_requires_auth(client: AsyncClient, seed: _Seed) -> None:
    assert (await client.post("/api/v1/attendance/clock-in")).status_code == 401


async def test_clock_in_out_flow(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    hdr = auth_headers(settings, seed.report)

    # Not clocked in yet.
    assert (await client.get("/api/v1/attendance/me", headers=hdr)).json() is None

    cin = await client.post("/api/v1/attendance/clock-in", headers=hdr)
    assert cin.status_code == 201, cin.text
    assert cin.json()["is_open"] is True
    assert cin.json()["clock_out_at"] is None

    # /me now reflects the open session.
    me = await client.get("/api/v1/attendance/me", headers=hdr)
    assert me.json()["id"] == cin.json()["id"]

    cout = await client.post("/api/v1/attendance/clock-out", headers=hdr)
    assert cout.status_code == 200
    assert cout.json()["is_open"] is False
    assert cout.json()["clock_out_at"] is not None

    # Closed → /me is null again.
    assert (await client.get("/api/v1/attendance/me", headers=hdr)).json() is None


async def test_clock_in_is_idempotent(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    hdr = auth_headers(settings, seed.report)
    first = await client.post("/api/v1/attendance/clock-in", headers=hdr)
    second = await client.post("/api/v1/attendance/clock-in", headers=hdr)
    assert first.json()["id"] == second.json()["id"]  # same open session


async def test_clock_out_without_clock_in_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/attendance/clock-out", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 422


async def test_attendance_board_reflects_clock_in(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The report clocks in; the manager's attendance board shows them present
    # with an explicit login time — even with no activity samples.
    await client.post("/api/v1/attendance/clock-in", headers=auth_headers(settings, seed.report))
    board = await client.get("/api/v1/attendance", headers=auth_headers(settings, seed.manager))
    assert board.status_code == 200
    row = next(r for r in board.json() if r["employee_id"] == str(seed.report.id))
    assert row["status"] in ("present", "late")
    assert row["login_at"] is not None
