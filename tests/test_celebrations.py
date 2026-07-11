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
    )

    # 4 active employees in the seed → the birthday is broadcast to all of them.
    sent = await service.run_daily(today)
    assert sent == 4
    assert all("Birthday" in str(c["subject"]) for c in calls)

    # Same day again → nothing re-sent (idempotent via last_run_on).
    assert await service.run_daily(today) == 0
