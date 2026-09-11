"""Celebrations — settings/festival authz, and the daily broadcast run."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.repositories.audit import AuditRepository
from app.repositories.celebration_settings import CelebrationSettingsRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.festival import FestivalRepository
from app.repositories.holiday import HolidayRepository
from app.services.celebration_service import CelebrationService
from app.services.email_service import EmailService
from tests.conftest import _Seed, auth_headers


async def test_settings_update_is_hr_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Everyone can read the toggles.
    assert (
        await client.get(
            "/api/v1/celebrations/settings", headers=auth_headers(settings, seed.report)
        )
    ).status_code == 200
    # A regular employee can't change them.
    denied = await client.put(
        "/api/v1/celebrations/settings",
        json={"birthday_enabled": False},
        headers=auth_headers(settings, seed.report),
    )
    assert denied.status_code == 403
    # Admin can.
    ok = await client.put(
        "/api/v1/celebrations/settings",
        json={"birthday_enabled": False},
        headers=auth_headers(settings, seed.admin),
    )
    assert ok.status_code == 200
    assert ok.json()["birthday_enabled"] is False


async def test_festival_crud_is_hr_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = {"name": "Diwali", "on_date": "2026-11-01", "message": "Happy Diwali!"}
    denied = await client.post(
        "/api/v1/celebrations/festivals", json=body, headers=auth_headers(settings, seed.report)
    )
    assert denied.status_code == 403

    created = await client.post(
        "/api/v1/celebrations/festivals", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert created.status_code == 201, created.text
    fid = created.json()["id"]

    listed = await client.get(
        "/api/v1/celebrations/festivals", headers=auth_headers(settings, seed.admin)
    )
    assert fid in [f["id"] for f in listed.json()]

    removed = await client.delete(
        f"/api/v1/celebrations/festivals/{fid}", headers=auth_headers(settings, seed.admin)
    )
    assert removed.status_code == 204


async def test_run_daily_broadcasts_and_is_idempotent(
    db: AsyncSession, seed: _Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send(self: EmailService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(EmailService, "send", fake_send)

    today = date(2026, 6, 22)
    seed.report.date_of_birth = date(1990, today.month, today.day)  # birthday today
    await db.commit()

    service = CelebrationService(
        CelebrationSettingsRepository(db),
        FestivalRepository(db),
        EmployeeRepository(db),
        EmailService(get_settings()),
        AuditRepository(db),
        HolidayRepository(db),
    )

    # ONE email now: addressed to the birthday person, with the rest of the team
    # CC'd — not a separate copy each (which is what `sent == 4` used to mean).
    sent = await service.run_daily(today)
    assert sent == 1
    assert len(calls) == 1
    assert all("Birthday" in str(c["subject"]) for c in calls)
    assert calls[0]["to"] == seed.report.work_email
    cc = calls[0]["cc"]
    assert isinstance(cc, list)
    # Everyone else is copied, and the birthday person is not double-mailed.
    assert seed.report.work_email not in cc
    assert seed.manager.work_email in cc

    # Same day again → nothing re-sent (idempotent via last_run_on).
    assert await service.run_daily(today) == 0


# --- the send hour ---------------------------------------------------------- #
def test_greetings_wait_for_the_configured_hour(settings: Settings) -> None:
    """A naive daily job fires the moment the date rolls over, so greetings went
    out at 00:0x. "Happy birthday" at five past midnight reads as a robot, so the
    worker holds until noon in the org's own timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    send_after = settings.celebrations_hour * 60 + settings.celebrations_minute
    assert send_after == 12 * 60  # noon, unless the env says otherwise

    def is_due(hour: int, minute: int) -> bool:
        now = datetime(2026, 9, 11, hour, minute, tzinfo=ist)
        return now.hour * 60 + now.minute >= send_after

    assert not is_due(0, 5)  # the bug this replaces
    assert not is_due(11, 59)
    assert is_due(12, 0)
    # Started late (a restart at 3 pm) still sends the day rather than skipping it.
    assert is_due(15, 30)
