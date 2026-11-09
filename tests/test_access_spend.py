"""Access spend - the finance view of what tools cost.

The maths has to match what finance would work out by hand (a yearly bill is a
twelfth a month, a one-time bill never recurs, rupees and dollars never mix),
and only the people who run the books may see org-wide spend or export it.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_spend import SpendInput, group_totals, renewals_within, to_line, totals
from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

BASE = "/api/v1/access"


def _item(**over: object) -> SpendInput:
    base: dict[str, object] = {
        "asset_id": "00000000-0000-0000-0000-000000000001",
        "name": "Vercel",
        "kind": "subscription",
        "category": "Hosting",
        "team": "Engineering",
        "owner_name": None,
        "billed_to": None,
        "status": "active",
        "cost_minor": 2_000,
        "currency": "USD",
        "billing_cycle": "Monthly",
        "renews_on": None,
        "payment_method": None,
        "card_last4": None,
        "auto_renew": None,
    }
    base.update(over)
    return SpendInput(**base)  # type: ignore[arg-type]


def test_each_cycle_turns_into_a_month_and_a_year() -> None:
    monthly = to_line(_item())
    assert (monthly.monthly_minor, monthly.annual_minor) == (2_000, 24_000)
    yearly = to_line(_item(cost_minor=1_20_000, currency="INR", billing_cycle="Yearly"))
    assert (yearly.monthly_minor, yearly.annual_minor) == (10_000, 1_20_000)
    usage = to_line(_item(billing_cycle="Usage-based"))
    assert usage.counted and usage.estimated and usage.annual_minor == 24_000
    once = to_line(_item(billing_cycle="One-time"))
    assert not once.recurring and not once.counted and once.annual_minor == 0


def test_rupees_and_dollars_are_totalled_apart_and_stopped_bills_left_out() -> None:
    lines = [
        to_line(_item()),
        to_line(_item(name="Canva", cost_minor=1_59_000, currency="INR", billing_cycle="Yearly")),
        to_line(_item(name="Old", status="cancelled")),
    ]
    t = totals(lines)
    assert t.monthly == {"USD": 2_000, "INR": 13_250}
    assert t.count == 2


def test_spend_groups_by_team_with_unassigned_last_resort() -> None:
    lines = [to_line(_item()), to_line(_item(name="X", team=None, cost_minor=500))]
    labels = [label for label, _ in group_totals(lines, "team")]
    assert labels == ["Engineering", "Unassigned"]


def test_renewals_are_the_next_ninety_days_soonest_first() -> None:
    today = date(2026, 11, 9)
    lines = [
        to_line(_item(name="Late", renews_on=date(2027, 3, 1))),
        to_line(_item(name="Soon", renews_on=date(2026, 11, 20))),
        to_line(_item(name="Mid", renews_on=date(2026, 12, 20))),
    ]
    assert [x.item.name for x in renewals_within(lines, today, 90)] == ["Soon", "Mid"]


async def _subscription(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        f"{BASE}/assets",
        json={
            "kind": "subscription",
            "name": "Canva",
            "cost_minor": 1_59_000,
            "currency": "INR",
            "billing_cycle": "Yearly",
            "details": {"team": "Design"},
        },
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text


async def test_only_finance_sees_or_exports_spend(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _subscription(client, settings, seed)
    for who in (seed.report, seed.manager):
        h = auth_headers(settings, who)
        assert (await client.get(f"{BASE}/spend", headers=h)).status_code == 403
        assert (await client.get(f"{BASE}/spend/export", headers=h)).status_code == 403

    finance = Employee(
        hr_external_id="hr-fin",
        work_email="finance@corp.test",
        full_name="Fin Ance",
        role=Role.EXECUTIVE,
        payroll_manager=True,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(finance)
    await db.commit()
    summary = await client.get(f"{BASE}/spend", headers=auth_headers(settings, finance))
    assert summary.status_code == 200
    body = summary.json()
    assert body["totals"] == [
        {"currency": "INR", "monthly_minor": 13_250, "annual_minor": 1_59_000}
    ]
    assert [g["label"] for g in body["by_team"]] == ["Design"]


async def test_the_export_is_a_workbook_finance_can_sum(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _subscription(client, settings, seed)
    resp = await client.get(f"{BASE}/spend/export", headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200
    assert "tech-spend-" in resp.headers["content-disposition"]
    wb = load_workbook(BytesIO(resp.content))
    assert wb.sheetnames == ["Summary", "Line items", "Renewals (90 days)"]
    items = wb["Line items"]
    header = [c.value for c in items[1]]
    row = {h: c.value for h, c in zip(header, items[2], strict=False)}
    assert row["Name"] == "Canva" and row["Annual"] == 1590 and row["Monthly"] == 132.5
