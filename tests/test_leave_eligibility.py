"""Leave eligibility rules: no past-dated leave, and gender/DOB-gated types."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Gender
from tests.conftest import _Seed, auth_headers


def _body(leave_type: str, start: datetime, *, end: datetime | None = None) -> dict[str, object]:
    end = end or start
    return {
        "leave_type": leave_type,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "reason": "x",
    }


async def test_cannot_apply_for_a_past_date(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # Even sick leave (which bypasses the notice rule) can't be backdated.
    start = datetime.now(UTC) - timedelta(days=1)
    resp = await client.post(
        "/api/v1/leaves", json=_body("sick", start), headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 422, resp.text


async def test_backdating_window_is_configurable(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # HR opens a 3-day backdating window.
    set_policy = await client.put(
        "/api/v1/leaves/policy",
        json={"max_backdate_days": 3},
        headers=auth_headers(settings, seed.admin),
    )
    assert set_policy.status_code == 200
    assert set_policy.json()["max_backdate_days"] == 3

    # A sick day that started 2 days ago is now allowed (within the window).
    within = await client.post(
        "/api/v1/leaves",
        json=_body("sick", datetime.now(UTC) - timedelta(days=2)),
        headers=auth_headers(settings, seed.report),
    )
    assert within.status_code == 201, within.text

    # 5 days ago is beyond the window — still rejected.
    beyond = await client.post(
        "/api/v1/leaves",
        json=_body("sick", datetime.now(UTC) - timedelta(days=5)),
        headers=auth_headers(settings, seed.report),
    )
    assert beyond.status_code == 422


async def test_maternity_requires_female(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    start = datetime.now(UTC) + timedelta(days=1)
    seed.report.gender = Gender.MALE
    await db.commit()
    denied = await client.post(
        "/api/v1/leaves",
        json=_body("maternity", start),
        headers=auth_headers(settings, seed.report),
    )
    assert denied.status_code == 422

    seed.report.gender = Gender.FEMALE
    await db.commit()
    allowed = await client.post(
        "/api/v1/leaves",
        json=_body("maternity", start),
        headers=auth_headers(settings, seed.report),
    )
    assert allowed.status_code == 201, allowed.text


async def test_paternity_requires_male(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    start = datetime.now(UTC) + timedelta(days=1)
    seed.report.gender = Gender.FEMALE
    await db.commit()
    denied = await client.post(
        "/api/v1/leaves",
        json=_body("paternity", start),
        headers=auth_headers(settings, seed.report),
    )
    assert denied.status_code == 422

    seed.report.gender = Gender.MALE
    await db.commit()
    allowed = await client.post(
        "/api/v1/leaves",
        json=_body("paternity", start),
        headers=auth_headers(settings, seed.report),
    )
    assert allowed.status_code == 201, allowed.text


async def test_birthday_needs_dob_in_birth_month(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    start = datetime.now(UTC) + timedelta(days=1)

    seed.report.date_of_birth = None
    await db.commit()
    no_dob = await client.post(
        "/api/v1/leaves", json=_body("birthday", start), headers=auth_headers(settings, seed.report)
    )
    assert no_dob.status_code == 422

    other_month = 1 if start.month != 1 else 2
    seed.report.date_of_birth = date(1990, other_month, 15)
    await db.commit()
    wrong_month = await client.post(
        "/api/v1/leaves", json=_body("birthday", start), headers=auth_headers(settings, seed.report)
    )
    assert wrong_month.status_code == 422

    seed.report.date_of_birth = date(1990, start.month, 15)
    await db.commit()
    ok = await client.post(
        "/api/v1/leaves", json=_body("birthday", start), headers=auth_headers(settings, seed.report)
    )
    assert ok.status_code == 201, ok.text
