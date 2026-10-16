"""Device capture health: agent auth, strict validation, incident alerts, reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import deps
from app.core.config import Settings
from app.core.ratelimit import FixedWindowRateLimiter
from app.core.security import generate_device_token, hash_device_token
from app.models import Device, Employee, EmployeeStatus, Role
from app.models.notification import Notification, NotificationKind
from tests.conftest import _Seed, agent_headers, auth_headers

URL = "/api/v1/devices/health"


def _report(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "agent_version": "1.4.2",
        "os": "darwin",
        "screen_capture": "ok",
        "window_info": "ok",
        "browser_url": "ok",
        "last_error": None,
        "queue_items": 0,
        "queue_bytes": 0,
        "oldest_queued_age_seconds": None,
    }
    body.update(overrides)
    return body


async def _post(client: AsyncClient, token: str, body: dict[str, object]) -> int:
    raw, headers = agent_headers(token, body)
    return (await client.post(URL, content=raw, headers=headers)).status_code


async def _device(db: AsyncSession, seed: _Seed) -> Device:
    row = await db.get(Device, seed.device.id, populate_existing=True)
    assert row is not None
    return row


async def _health_notes(db: AsyncSession) -> list[Notification]:
    rows = await db.scalars(
        select(Notification)
        .where(Notification.kind == NotificationKind.DEVICE_HEALTH)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _add_it_admin(db: AsyncSession) -> Employee:
    it_admin = Employee(
        hr_external_id="hr-it",
        work_email="it@corp.test",
        full_name="Ivy IT",
        role=Role.IT_ADMIN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(it_admin)
    await db.commit()
    return it_admin


async def _second_device(db: AsyncSession, settings: Settings, seed: _Seed) -> tuple[Device, str]:
    token = generate_device_token()
    other = Device(
        employee_id=seed.outsider.id,
        label="outsider-laptop",
        token_hash=hash_device_token(settings, token),
        last_sequence=0,
    )
    db.add(other)
    await db.commit()
    return other, token


# --------------------------------------------------------------------------- #
# Authentication: same device token + HMAC + rate limit as activity ingest.
# --------------------------------------------------------------------------- #
async def test_no_token_is_401(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post(URL, json=_report())
    assert resp.status_code == 401


async def test_user_jwt_is_not_device_auth(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(URL, json=_report(), headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 401


async def test_bad_hmac_is_rejected(client: AsyncClient, db: AsyncSession, seed: _Seed) -> None:
    raw, headers = agent_headers(seed.device_raw_token, _report(screen_capture="failing"))
    headers["X-Signature"] = "sha256=deadbeef"
    assert (await client.post(URL, content=raw, headers=headers)).status_code == 401
    assert (await _device(db, seed)).health_reported_at is None


async def test_other_device_cannot_write(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    other, other_token = await _second_device(db, settings, seed)
    # Signed with the victim's token but presented under another device's
    # bearer: the HMAC is checked against the bearer's own token, so it fails.
    raw, headers = agent_headers(seed.device_raw_token, _report(screen_capture="failing"))
    headers["Authorization"] = f"Bearer {other_token}"
    assert (await client.post(URL, content=raw, headers=headers)).status_code == 401

    # A device's own valid report only ever lands on its own row.
    assert await _post(client, other_token, _report(screen_capture="failing")) == 204
    assert (await _device(db, seed)).health_reported_at is None
    stored = await db.get(Device, other.id, populate_existing=True)
    assert stored is not None and stored.health_screen_capture == "failing"


async def test_revoked_device_is_rejected(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    device = await _device(db, seed)
    device.is_revoked = True
    await db.commit()
    assert await _post(client, seed.device_raw_token, _report()) == 401


async def test_rate_limited_like_other_device_routes(
    client: AsyncClient, seed: _Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "_ingest_limiter", FixedWindowRateLimiter(max_requests=1))
    assert await _post(client, seed.device_raw_token, _report()) == 204
    assert await _post(client, seed.device_raw_token, _report()) == 403


# --------------------------------------------------------------------------- #
# Validation: strict, extra fields forbidden, bounded numbers.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"device_id": "x"},  # extra field
        {"os": "android"},
        {"screen_capture": "broken"},
        {"window_info": "unsupported"},  # only browser_url may be unsupported
        {"browser_url": "nope"},
        {"queue_items": -1},
        {"queue_bytes": -5},
        {"oldest_queued_age_seconds": -1},
        {"queue_items": "5"},  # strict: no string coercion
        {"queue_items": True},  # strict: no bool-as-int
        {"agent_version": "x" * 33},
        {"agent_version": "   "},
        {"last_error": "e" * 301},
    ],
)
async def test_invalid_payloads_are_422(
    client: AsyncClient, seed: _Seed, overrides: dict[str, object]
) -> None:
    assert await _post(client, seed.device_raw_token, _report(**overrides)) == 422


async def test_missing_field_is_422(client: AsyncClient, seed: _Seed) -> None:
    body = _report()
    del body["queue_items"]
    assert await _post(client, seed.device_raw_token, body) == 422


async def test_report_is_stored_with_server_time_and_clean_error(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    before = datetime.now(UTC)
    body = _report(last_error="  capture\x00 failed\n\tagain  ", queue_items=3, queue_bytes=2048)
    assert await _post(client, seed.device_raw_token, body) == 204
    device = await _device(db, seed)
    assert device.health_agent_version == "1.4.2"
    assert device.health_last_error == "capture failedagain"
    assert device.health_queue_items == 3
    assert device.health_queue_bytes == 2048
    assert device.health_reported_at is not None
    assert device.health_reported_at.replace(tzinfo=UTC) >= before - timedelta(seconds=1)


async def test_blank_error_is_stored_as_null(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    assert await _post(client, seed.device_raw_token, _report(last_error=" \n ")) == 204
    assert (await _device(db, seed)).health_last_error is None


# --------------------------------------------------------------------------- #
# Transitions: one alert per incident, one quiet note on recovery.
# --------------------------------------------------------------------------- #
async def test_failure_alerts_admins_and_owner_once(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    it_admin = await _add_it_admin(db)
    failing = _report(screen_capture="permission_denied")
    assert await _post(client, seed.device_raw_token, failing) == 204

    notes = await _health_notes(db)
    recipients = sorted(str(n.recipient_id) for n in notes)
    assert recipients == sorted([str(seed.admin.id), str(it_admin.id), str(seed.report.id)])
    owner_note = next(n for n in notes if n.recipient_id == seed.report.id)
    assert owner_note.body is not None and "screenshots" in owner_note.body
    admin_note = next(n for n in notes if n.recipient_id == seed.admin.id)
    assert admin_note.link == "/dashboard/admin/devices"
    assert admin_note.body is not None and "Screen recording permission" in admin_note.body
    assert (await _device(db, seed)).health_attention_since is not None

    # Still failing on the next reports: no repeat.
    assert await _post(client, seed.device_raw_token, failing) == 204
    assert await _post(client, seed.device_raw_token, _report(window_info="empty")) == 204
    assert len(await _health_notes(db)) == 3


async def test_recovery_sends_one_quiet_note_to_admins(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    assert await _post(client, seed.device_raw_token, _report(screen_capture="failing")) == 204
    assert len(await _health_notes(db)) == 2  # admin + owner

    assert await _post(client, seed.device_raw_token, _report()) == 204
    assert await _post(client, seed.device_raw_token, _report()) == 204
    notes = await _health_notes(db)
    recoveries = [n for n in notes if n.entity_type == "device_recovered"]
    assert [n.recipient_id for n in recoveries] == [seed.admin.id]
    assert recoveries[0].level.value == "info"
    assert (await _device(db, seed)).health_attention_since is None


async def test_healthy_reports_never_notify(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    # "unsupported" / "unknown" browser URL is fine (some browsers expose none).
    for browser_url in ("ok", "unsupported", "unknown"):
        body = _report(browser_url=browser_url, screen_capture="unknown", window_info="unknown")
        assert await _post(client, seed.device_raw_token, body) == 204
    assert await _health_notes(db) == []


async def test_queue_backlog_over_an_hour_needs_attention(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    ok_backlog = _report(queue_items=5, oldest_queued_age_seconds=3600)
    assert await _post(client, seed.device_raw_token, ok_backlog) == 204
    assert await _health_notes(db) == []

    stuck = _report(queue_items=500, oldest_queued_age_seconds=3601)
    assert await _post(client, seed.device_raw_token, stuck) == 204
    assert len(await _health_notes(db)) == 2


# --------------------------------------------------------------------------- #
# Stale report: the agent keeps sending activity but stopped reporting health.
# --------------------------------------------------------------------------- #
def _sample(sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "client_timestamp": datetime.now(UTC).isoformat(),
        "active_window": "Code.app",
        "idle_seconds": 0,
    }


async def test_stale_report_with_live_activity_alerts_once(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    assert await _post(client, seed.device_raw_token, _report()) == 204
    device = await _device(db, seed)
    device.health_reported_at = datetime.now(UTC) - timedelta(minutes=31)
    await db.commit()

    for seq in (1, 2):
        raw, headers = agent_headers(seed.device_raw_token, _sample(seq))
        resp = await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
        assert resp.status_code == 202
    assert len(await _health_notes(db)) == 2  # admin + owner, once

    listed = await client.get("/api/v1/devices", headers=auth_headers(settings, seed.admin))
    health = next(d for d in listed.json() if d["id"] == str(seed.device.id))["health"]
    assert health["status"] == "needs_attention"
    assert [i["code"] for i in health["issues"]] == ["report_stale"]


async def test_recent_report_with_activity_is_not_stale(
    client: AsyncClient, db: AsyncSession, seed: _Seed
) -> None:
    assert await _post(client, seed.device_raw_token, _report()) == 204
    raw, headers = agent_headers(seed.device_raw_token, _sample(1))
    assert (
        await client.post("/api/v1/activity/ingest", content=raw, headers=headers)
    ).status_code == 202
    assert await _health_notes(db) == []


# --------------------------------------------------------------------------- #
# Reads: health rides on the existing scoped device list.
# --------------------------------------------------------------------------- #
async def _health_for(
    client: AsyncClient, settings: Settings, viewer: Employee, device_id: object
) -> dict[str, object] | None:
    resp = await client.get("/api/v1/devices", headers=auth_headers(settings, viewer))
    assert resp.status_code == 200
    for row in resp.json():
        if row["id"] == str(device_id):
            health: dict[str, object] = row["health"]
            return health
    return None


async def test_read_scoping_and_error_visibility(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = _report(screen_capture="failing", last_error="CGWindowList returned nil")
    assert await _post(client, seed.device_raw_token, body) == 204

    admin_view = await _health_for(client, settings, seed.admin, seed.device.id)
    assert admin_view is not None
    assert admin_view["status"] == "needs_attention"
    assert admin_view["last_error"] == "CGWindowList returned nil"
    assert admin_view["agent_version"] == "1.4.2"

    # The manager sees their report's device (existing scope) but not the raw error.
    manager_view = await _health_for(client, settings, seed.manager, seed.device.id)
    assert manager_view is not None and manager_view["status"] == "needs_attention"
    assert manager_view["last_error"] is None

    # The owner sees their own device's health.
    owner_view = await _health_for(client, settings, seed.report, seed.device.id)
    assert owner_view is not None and owner_view["status"] == "needs_attention"

    # An out-of-scope employee sees nothing at all.
    assert await _health_for(client, settings, seed.outsider, seed.device.id) is None


async def test_device_without_a_report_is_no_report(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    health = await _health_for(client, settings, seed.admin, seed.device.id)
    assert health is not None
    assert health["status"] == "no_report"
    assert health["issues"] == []
