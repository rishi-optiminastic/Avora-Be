"""Location-restricted clock-in — office geofences, the org toggle, hard-block
enforcement, and the per-employee exemption.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

# A single office at (19.0, 72.8), 150 m radius.
_OFFICE = {"name": "HQ Mumbai", "latitude": 19.0, "longitude": 72.8, "radius_m": 150}
_INSIDE = {"latitude": 19.0001, "longitude": 72.8001}  # ~14 m away
_OUTSIDE = {"latitude": 19.01, "longitude": 72.81}  # ~1.4 km away


async def _require_location(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    h = auth_headers(settings, seed.admin)
    r = await client.put(
        "/api/v1/attendance/policy",
        json={"require_location_for_clock_in": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/attendance/office-locations", json=_OFFICE, headers=h)
    assert r.status_code == 201, r.text


async def test_office_locations_are_hr_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        denied = await client.post(
            "/api/v1/attendance/office-locations",
            json=_OFFICE,
            headers=auth_headers(settings, actor),
        )
        assert denied.status_code == 403
        listed = await client.get(
            "/api/v1/attendance/office-locations", headers=auth_headers(settings, actor)
        )
        assert listed.status_code == 403

    ok = await client.post(
        "/api/v1/attendance/office-locations",
        json=_OFFICE,
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 201, ok.text


async def test_clock_in_inside_geofence_succeeds(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _require_location(client, settings, seed)
    resp = await client.post(
        "/api/v1/attendance/clock-in", json=_INSIDE, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_open"] is True


async def test_clock_in_outside_geofence_is_blocked(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _require_location(client, settings, seed)
    resp = await client.post(
        "/api/v1/attendance/clock-in", json=_OUTSIDE, headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 422
    assert "office" in resp.text.lower()
    # Nothing was created.
    assert (
        await client.get("/api/v1/attendance/me", headers=auth_headers(settings, seed.report))
    ).json() is None


async def test_clock_in_without_coords_is_blocked_when_required(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _require_location(client, settings, seed)
    resp = await client.post(
        "/api/v1/attendance/clock-in", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 422
    assert "location" in resp.text.lower()


async def test_exempt_employee_clocks_in_from_anywhere(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _require_location(client, settings, seed)
    exempt = await client.put(
        f"/api/v1/employees/{seed.report.id}/location-exempt",
        json={"location_check_exempt": True},
        headers=auth_headers(settings, seed.admin),
    )
    assert exempt.status_code == 200, exempt.text
    # Outside the office, no coords — still allowed because exempt.
    resp = await client.post(
        "/api/v1/attendance/clock-in", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 201, resp.text


async def test_clock_in_unrestricted_by_default(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # No policy toggle, no offices — clock-in works with no coordinates.
    resp = await client.post(
        "/api/v1/attendance/clock-in", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 201, resp.text
