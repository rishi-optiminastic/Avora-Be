"""Payroll — the salary calculator, the org estimate, and HR/Admin-only authz.

The calculator is tested as a pure function (it must reproduce the reference
₹50,000 slip exactly). The API tests prove the wiring and, per CLAUDE §9, that
no one outside HR/Admin can reach org-wide payroll.
"""

from __future__ import annotations

from datetime import date, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.payroll import (
    CalcConfig,
    compute_breakdown,
    days_in_month,
    prorate_breakdown,
    weekdays_in_month,
    working_days_between,
)
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

# ---- pure calculator (the reference ₹50,000 structure, in paise) ----------- #


def test_working_days_per_week_counts_from_monday() -> None:
    # Any 7 consecutive days contain each weekday exactly once, so the count in a
    # full week equals the configured working-days-per-week (weekday-agnostic).
    start = date(2026, 6, 1)
    week = (start, start + timedelta(days=6))
    assert working_days_between(*week, set()) == 5  # default (Mon-Fri)
    assert working_days_between(*week, set(), 6) == 6  # Mon-Sat
    assert working_days_between(*week, set(), 7) == 7  # every day
    # June 2026 has 30 days; wdpw=7 counts them all, and Mon-Sat ≥ Mon-Fri.
    assert len(weekdays_in_month(2026, 6, 7)) == 30
    assert len(weekdays_in_month(2026, 6, 6)) >= len(weekdays_in_month(2026, 6, 5))


def test_breakdown_matches_reference_slip() -> None:
    b = compute_breakdown(50_000_00)  # ₹50,000/mo CTC
    assert b.basic_minor == 15_000_00
    assert b.hra_minor == 7_500_00
    assert b.special_allowance_minor == 25_700_00
    assert b.gross_minor == 48_200_00
    assert b.employer_pf_minor == 1_800_00
    assert b.employee_pf_minor == 1_800_00
    assert b.professional_tax_minor == 200_00
    assert b.total_deduction_minor == 2_000_00
    assert b.net_minor == 46_200_00


def test_breakdown_zero_ctc_is_all_zero() -> None:
    b = compute_breakdown(0)
    assert b.net_minor == 0 and b.gross_minor == 0 and b.basic_minor == 0


def test_breakdown_honours_config_knobs() -> None:
    b = compute_breakdown(50_000_00, CalcConfig(basic_pct=40, professional_tax_minor=0))
    assert b.basic_minor == 20_000_00
    assert b.professional_tax_minor == 0


def test_prorate_breakdown_matches_reference_sheet() -> None:
    # The reference "Salary Breakdown" sheet: ₹40,000/mo CTC, 26 of 30 days paid.
    b = compute_breakdown(40_000_00)
    assert b.gross_minor == 38_560_00  # full-month gross
    assert b.net_minor == 36_920_00  # full-month net (no TDS at this income)

    p = prorate_breakdown(b, payable_days=26, total_days=30)
    # Earnings + both PF figures scale by 26/30.
    assert p.basic_minor == 10_400_00
    assert p.hra_minor == 5_200_00
    assert p.special_allowance_minor == 17_818_67  # 20,560 * 26/30, to the paise
    assert p.gross_minor == 33_418_67  # displays as ₹33,419 (rounded)
    assert p.employee_pf_minor == 1_248_00
    # Professional tax stays FLAT — it is not attendance-linked.
    assert p.professional_tax_minor == 200_00
    assert p.income_tax_minor == 0
    assert p.total_deduction_minor == 1_448_00
    assert p.net_minor == 31_970_67  # displays as ₹31,971 (rounded)


def test_prorate_breakdown_edges() -> None:
    b = compute_breakdown(40_000_00)
    assert prorate_breakdown(b, 30, 30).net_minor == b.net_minor  # full month
    assert prorate_breakdown(b, 0, 30).gross_minor == 0  # nothing payable
    assert prorate_breakdown(b, 40, 30).net_minor == b.net_minor  # capped at full
    assert prorate_breakdown(b, 10, 0).net_minor == b.net_minor  # no days -> full
    assert days_in_month(2026, 2) == 28 and days_in_month(2026, 6) == 30


# ---- estimate over the org ------------------------------------------------- #

_COMP = {"amount_minor": 50_000_00, "currency": "inr", "period": "monthly"}
_SETTINGS = {
    "pay_day_of_month": 1,
    "currency": "INR",
    "pay_cycle": "monthly",
    "auto_send_enabled": False,
    "recipients": [],
    "basic_pct": 30,
    "hra_pct": 50,
    "pf_pct": 12,
    "pf_cap_minor": 1_800_00,
    "professional_tax_minor": 200_00,
    "professional_tax_feb_minor": 300_00,
    "deduct_income_tax": True,
}


async def test_estimate_computes_slip_and_total(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    await client.put(
        "/api/v1/payroll/settings", json=_SETTINGS, headers=auth_headers(settings, seed.admin)
    )

    resp = await client.get(
        "/api/v1/payroll/estimate?month=2026-06", headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency"] == "INR"
    assert body["total_days"] == 30  # June 2026 is a 30-day month
    assert body["working_days"] > 0
    assert body["employee_count"] == len(body["lines"])
    assert body["total_net_minor"] == sum(line["net_minor"] for line in body["lines"])

    report_line = next(line for line in body["lines"] if line["employee_id"] == str(seed.report.id))
    # The full-month slip is independent of attendance and must match the image.
    assert report_line["monthly_ctc_minor"] == 50_000_00
    assert report_line["breakdown"]["net_minor"] == 46_200_00
    assert report_line["missing_compensation"] is False
    # No work sessions seeded -> every working day is loss-of-pay, but weekends and
    # holidays are still paid, so the prorated take-home is a fraction, not zero.
    assert report_line["net_minor"] == report_line["prorated"]["net_minor"]
    assert 0 < report_line["net_minor"] < 46_200_00
    assert report_line["payable_days"] == 30 - report_line["working_days"]

    outsider_line = next(
        line for line in body["lines"] if line["employee_id"] == str(seed.outsider.id)
    )
    assert outsider_line["missing_compensation"] is True
    assert outsider_line["monthly_ctc_minor"] == 0
    assert outsider_line["breakdown"]["net_minor"] == 0


async def test_hr_can_read_estimate(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = Employee(
        hr_external_id="hr-person",
        work_email="hr@corp.test",
        full_name="Hank HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()

    resp = await client.get(
        "/api/v1/payroll/estimate?month=2026-06", headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 200


# ---- excel export ---------------------------------------------------------- #

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def test_export_xlsx_hr_admin_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    ok = await client.get(
        "/api/v1/payroll/export?month=2026-06", headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 200
    assert ok.headers["content-type"] == _XLSX_MEDIA_TYPE
    assert ok.content[:2] == b"PK"  # xlsx is a zip archive
    assert "payroll-2026-06.xlsx" in ok.headers["content-disposition"]

    for actor in (seed.report, seed.manager, seed.outsider):
        forbidden = await client.get(
            "/api/v1/payroll/export?month=2026-06", headers=auth_headers(settings, actor)
        )
        assert forbidden.status_code == 403, f"{actor.work_email} must not export payroll"


# ---- authorization: payroll is HR/Admin only (CLAUDE §9) ------------------- #


async def test_estimate_forbidden_for_non_hr_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(
            "/api/v1/payroll/estimate?month=2026-06", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 403, f"{actor.work_email} should not read payroll"


async def test_settings_and_send_forbidden_for_non_hr_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    get_settings = await client.get(
        "/api/v1/payroll/settings", headers=auth_headers(settings, seed.manager)
    )
    assert get_settings.status_code == 403

    put_settings = await client.put(
        "/api/v1/payroll/settings", json=_SETTINGS, headers=auth_headers(settings, seed.manager)
    )
    assert put_settings.status_code == 403

    send = await client.post(
        "/api/v1/payroll/send?month=2026-06", headers=auth_headers(settings, seed.report)
    )
    assert send.status_code == 403

    runs = await client.get("/api/v1/payroll/runs", headers=auth_headers(settings, seed.outsider))
    assert runs.status_code == 403


# ---- send digest ----------------------------------------------------------- #


async def test_send_requires_recipients_then_records_run(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # No recipients configured yet -> a helpful 422, not a silent no-op.
    empty = await client.post(
        "/api/v1/payroll/send?month=2026-06", headers=auth_headers(settings, seed.admin)
    )
    assert empty.status_code == 422

    await client.put(
        "/api/v1/payroll/settings",
        json={**_SETTINGS, "recipients": ["finance@corp.test", "hr@corp.test"]},
        headers=auth_headers(settings, seed.admin),
    )
    sent = await client.post(
        "/api/v1/payroll/send?month=2026-06", headers=auth_headers(settings, seed.admin)
    )
    assert sent.status_code == 200
    run = sent.json()
    assert run["period_month"] == "2026-06"
    assert run["source"] == "manual"
    assert run["recipients"] == ["finance@corp.test", "hr@corp.test"]

    runs = await client.get("/api/v1/payroll/runs", headers=auth_headers(settings, seed.admin))
    assert runs.status_code == 200
    assert any(r["period_month"] == "2026-06" for r in runs.json())
