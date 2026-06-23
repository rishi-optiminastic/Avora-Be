"""Agent-fleet nudge — POST /devices/nudge.

Authz mirrors pings (manager+ and in-scope, else 403/404). The channel is chosen
by whether the agent is reporting: online → agent ping; else notification + email.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.device import Device
from app.models.notification import Notification, NotificationKind
from app.models.ping import Ping, PingKind
from tests.conftest import _Seed, auth_headers


async def _set_seed_device_last_seen(db: AsyncSession, employee_id: object, when: datetime) -> None:
    row = await db.execute(select(Device).where(Device.employee_id == employee_id))
    device = row.scalars().first()
    assert device is not None
    device.last_seen_at = when
    await db.commit()


async def test_employee_cannot_nudge(client: AsyncClient, seed: _Seed, settings: Settings) -> None:
    # A plain employee is not a manager → 403.
    resp = await client.post(
        "/api/v1/devices/nudge",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.report),
    )
    assert resp.status_code == 403


async def test_out_of_scope_target_is_404(
    client: AsyncClient, seed: _Seed, settings: Settings
) -> None:
    # The manager can't nudge someone outside their scope (outsider is not a report).
    resp = await client.post(
        "/api/v1/devices/nudge",
        json={"employee_id": str(seed.outsider.id)},
        headers=auth_headers(settings, seed.manager),
    )
    assert resp.status_code == 404


async def test_online_device_sends_agent_ping(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    await _set_seed_device_last_seen(db, seed.report.id, datetime.now(UTC))
    resp = await client.post(
        "/api/v1/devices/nudge",
        json={"employee_id": str(seed.report.id)},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["channel"] == "agent"

    pings = (
        (await db.execute(select(Ping).where(Ping.target_employee_id == seed.report.id)))
        .scalars()
        .all()
    )
    assert any(p.kind is PingKind.PING for p in pings)


async def test_offline_device_notifies_and_emails(
    client: AsyncClient, db: AsyncSession, seed: _Seed, settings: Settings
) -> None:
    # Device last seen long ago → treated as offline → notification + email path.
    await _set_seed_device_last_seen(db, seed.report.id, datetime.now(UTC) - timedelta(hours=5))
    resp = await client.post(
        "/api/v1/devices/nudge",
        json={"employee_id": str(seed.report.id), "message": "Please reinstall Avora"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.json()["channel"] == "notification_email"

    notifs = (
        (await db.execute(select(Notification).where(Notification.recipient_id == seed.report.id)))
        .scalars()
        .all()
    )
    assert any(n.kind is NotificationKind.SYSTEM for n in notifs)
