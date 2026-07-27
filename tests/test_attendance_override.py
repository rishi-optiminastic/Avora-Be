"""Attendance overrides — HR/Admin-only management + effect on the day view.

Proves the authz gate (CLAUDE §9), that one override exists per (employee, day),
and that an override forces the status in the attendance range (which drives the
payroll present/absent counts).
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_DAY = "2026-06-15"
_MONTH = "2026-06"


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "employee_id": "",  # filled by caller
        "day": _DAY,
        "status": "full_day",
    }
    body.update(over)
    return body


async def test_only_hr_admin_manages_overrides(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = _body(employee_id=str(seed.report.id))
    for actor in (seed.report, seed.manager, seed.outsider):
        denied = await client.post(
            "/api/v1/attendance/overrides", json=body, headers=auth_headers(settings, actor)
        )
        assert denied.status_code == 403
        listed = await client.get(
            f"/api/v1/attendance/overrides?month={_MONTH}", headers=auth_headers(settings, actor)
        )
        assert listed.status_code == 403

    ok = await client.post(
        "/api/v1/attendance/overrides", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 201, ok.text
    override_id = ok.json()["id"]
    assert (
        await client.delete(
            f"/api/v1/attendance/overrides/{override_id}",
            headers=auth_headers(settings, seed.report),
        )
    ).status_code == 403
    gone = await client.delete(
        f"/api/v1/attendance/overrides/{override_id}", headers=auth_headers(settings, seed.admin)
    )
    assert gone.status_code == 204


async def test_one_override_per_day_replaces(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    h = auth_headers(settings, seed.admin)
    await client.post(
        "/api/v1/attendance/overrides", json=_body(employee_id=str(seed.report.id)), headers=h
    )
    await client.post(
        "/api/v1/attendance/overrides",
        json=_body(employee_id=str(seed.report.id), status="absent"),
        headers=h,
    )
    listed = await client.get(f"/api/v1/attendance/overrides?month={_MONTH}", headers=h)
    rows = [r for r in listed.json() if r["employee_id"] == str(seed.report.id)]
    assert len(rows) == 1  # the second set replaced the first
    assert rows[0]["status"] == "absent"


async def test_override_forces_status_in_range(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # No sessions seeded → the report would be absent (no row) on _DAY. The override
    # to full_day must synthesise a full_day row for that day.
    await client.post(
        "/api/v1/attendance/overrides",
        json=_body(employee_id=str(seed.report.id), status="full_day"),
        headers=auth_headers(settings, seed.admin),
    )
    resp = await client.get(
        f"/api/v1/attendance/range?start={_DAY}&end={_DAY}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    report_rows = [r for r in resp.json() if r["employee_id"] == str(seed.report.id)]
    assert len(report_rows) == 1
    assert report_rows[0]["status"] == "full_day"
    assert report_rows[0]["regularized"] is True
