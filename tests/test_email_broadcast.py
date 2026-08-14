"""Broadcast email: CC handling, chunking, and the org-wide sends.

The CC path is what turns "one mail each" into "one mail, everyone visible" — it
is also the only thing standing between a company-wide announcement and Gmail's
per-message recipient cap, so the chunking is pinned here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.holiday import Holiday, HolidayType
from app.repositories.audit import AuditRepository
from app.repositories.celebration_settings import CelebrationSettingsRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.festival import FestivalRepository
from app.repositories.holiday import HolidayRepository
from app.services.celebration_service import CelebrationService
from app.services.email_service import MAX_CC_PER_MESSAGE, EmailService, _dedupe_cc
from tests.conftest import _FakeEmailService, _Seed, auth_headers


# --- CC list hygiene -------------------------------------------------------- #
def test_cc_drops_the_to_address() -> None:
    """The subject of the mail must not also be CC'd — one person, one copy."""
    assert _dedupe_cc("me@corp.test", ["me@corp.test", "you@corp.test"]) == ["you@corp.test"]


def test_cc_dedupes_case_insensitively() -> None:
    # Same mailbox, different casing — mailing them twice looks like a bug.
    assert _dedupe_cc("a@corp.test", ["B@corp.test", "b@corp.test", "  b@corp.test "]) == [
        "B@corp.test"
    ]


def test_cc_drops_blanks() -> None:
    assert _dedupe_cc("a@corp.test", ["", "   ", "c@corp.test"]) == ["c@corp.test"]


# --- chunking --------------------------------------------------------------- #
async def test_broadcast_chunks_a_large_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audience over the per-message cap is split, and the To line repeats on
    every chunk so the subject of the mail always receives it."""
    calls: list[dict[str, object]] = []

    async def fake_send(self: EmailService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(EmailService, "send", fake_send)

    audience = [f"person{i}@corp.test" for i in range(MAX_CC_PER_MESSAGE * 2 + 5)]
    await EmailService(get_settings()).send_broadcast(
        to="star@corp.test", audience=audience, subject="Hi", html="<p>Hi</p>"
    )

    assert len(calls) == 3  # 90 + 90 + 5
    assert all(call["to"] == "star@corp.test" for call in calls)
    assert sum(len(call["cc"]) for call in calls) == len(audience)  # type: ignore[arg-type]


async def test_broadcast_with_no_audience_still_sends_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-person company still gets their birthday email."""
    calls: list[dict[str, object]] = []

    async def fake_send(self: EmailService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(EmailService, "send", fake_send)
    await EmailService(get_settings()).send_broadcast(
        to="solo@corp.test", audience=["solo@corp.test"], subject="Hi", html="<p>Hi</p>"
    )
    assert len(calls) == 1
    assert calls[0].get("cc") is None


# --- holiday reminder ------------------------------------------------------- #
def _service(db: AsyncSession) -> CelebrationService:
    return CelebrationService(
        CelebrationSettingsRepository(db),
        FestivalRepository(db),
        EmployeeRepository(db),
        EmailService(get_settings()),
        AuditRepository(db),
        HolidayRepository(db),
    )


async def test_holiday_reminder_goes_out_the_day_before(
    db: AsyncSession, seed: _Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send(self: EmailService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(EmailService, "send", fake_send)

    today = date(2026, 6, 22)
    db.add(
        Holiday(
            name="Founders Day", date=today + timedelta(days=1), holiday_type=HolidayType.PUBLIC
        )
    )
    await db.commit()

    sent = await _service(db).run_daily(today)

    assert sent == 1
    assert "Founders Day" in str(calls[0]["subject"])
    # The whole active roster is CC'd — people already on leave included, since
    # "the office is closed tomorrow" is just as relevant to them.
    cc = calls[0]["cc"]
    assert isinstance(cc, list)
    assert seed.report.work_email in cc


async def test_no_holiday_reminder_on_the_holiday_itself(
    db: AsyncSession, seed: _Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reminder the morning OF is useless — the office is already closed."""
    calls: list[dict[str, object]] = []

    async def fake_send(self: EmailService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(EmailService, "send", fake_send)

    today = date(2026, 6, 22)
    db.add(Holiday(name="Today Only", date=today, holiday_type=HolidayType.PUBLIC))
    await db.commit()

    assert await _service(db).run_daily(today) == 0
    assert calls == []


# --- announcement ----------------------------------------------------------- #
async def test_posting_an_announcement_emails_the_whole_team(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        "/api/v1/announcements",
        json={"message": "All-hands moved to Friday.", "level": "info"},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code in (200, 201), resp.text

    mailed = {to for kind, to in _FakeEmailService.outbox if kind == "announcement"}
    assert seed.report.work_email in mailed
    assert seed.manager.work_email in mailed
