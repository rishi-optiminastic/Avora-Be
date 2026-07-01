"""Announcements — HR/Admin author, everyone reads, holiday notices auto-derive.

Authz per CLAUDE §9: posting/retiring is HR/Admin only; reading is open to any
authenticated employee (the dashboard bar).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.holiday import Holiday, HolidayType
from tests.conftest import _Seed, auth_headers


async def test_create_forbidden_for_non_hr_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            "/api/v1/announcements",
            json={"message": "Sneaky notice", "level": "info"},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.work_email} must not post announcements"


async def test_admin_posts_and_everyone_reads(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/announcements",
        json={"message": "All-hands at 4pm IST", "level": "warning"},
        headers=auth_headers(settings, seed.admin),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "custom"
    assert body["level"] == "warning"

    # A regular employee sees it in the bar.
    listed = await client.get("/api/v1/announcements", headers=auth_headers(settings, seed.report))
    assert listed.status_code == 200
    assert any(a["message"] == "All-hands at 4pm IST" for a in listed.json())


async def test_holiday_tomorrow_appears_automatically(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # Cover today / tomorrow across any org timezone (UTC .. UTC+5:30) by seeding
    # a 3-day window; the service emits a notice for today or tomorrow (org tz).
    base = datetime.now(UTC).date()
    for offset in (0, 1, 2):
        db.add(
            Holiday(
                name="Founders Day",
                date=base + timedelta(days=offset),
                holiday_type=HolidayType.PUBLIC,
            )
        )
    await db.commit()

    listed = await client.get("/api/v1/announcements", headers=auth_headers(settings, seed.report))
    assert listed.status_code == 200
    holiday_items = [a for a in listed.json() if a["kind"] == "holiday"]
    assert holiday_items, "a holiday today/tomorrow should surface a derived announcement"
    assert any("Founders Day" in a["message"] for a in holiday_items)


async def test_delete_forbidden_then_admin_retires(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    created = await client.post(
        "/api/v1/announcements",
        json={"message": "Temporary notice"},
        headers=auth_headers(settings, seed.admin),
    )
    aid = created.json()["id"]

    denied = await client.delete(
        f"/api/v1/announcements/{aid}", headers=auth_headers(settings, seed.manager)
    )
    assert denied.status_code == 403

    ok = await client.delete(
        f"/api/v1/announcements/{aid}", headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 204

    listed = await client.get("/api/v1/announcements", headers=auth_headers(settings, seed.report))
    assert all(a["id"] != aid for a in listed.json())
