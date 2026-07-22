"""My Pay — the self-service payslip endpoint (`GET /payroll/me`).

Unlike the org-wide `/payroll/estimate` (HR/Admin only), every employee may read
their OWN slip here, and only their own — the endpoint never takes an id, so one
person can't reach another's pay. The numbers must match that person's line in
the HR estimate exactly (shared computation, no drift).
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

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
}


async def test_my_slip_returns_own_pay(
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
        "/api/v1/payroll/me?month=2026-06", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    slip = resp.json()
    assert slip["month"] == "2026-06"
    assert slip["currency"] == "INR"
    assert slip["monthly_ctc_minor"] == 50_000_00
    # Full-month slip is attendance-independent and matches the reference image.
    assert slip["breakdown"]["net_minor"] == 46_200_00
    assert slip["missing_compensation"] is False
    # No work sessions seeded -> working days are loss-of-pay, but weekends and
    # holidays stay paid, so the prorated take-home is a fraction, not zero.
    assert slip["net_minor"] == slip["prorated"]["net_minor"]
    assert 0 < slip["net_minor"] < 46_200_00
    assert slip["payable_days"] == slip["total_days"] - slip["working_days"]


async def test_my_slip_matches_estimate_line(
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

    est = (
        await client.get(
            "/api/v1/payroll/estimate?month=2026-06", headers=auth_headers(settings, seed.admin)
        )
    ).json()
    line = next(line for line in est["lines"] if line["employee_id"] == str(seed.report.id))

    slip = (
        await client.get(
            "/api/v1/payroll/me?month=2026-06", headers=auth_headers(settings, seed.report)
        )
    ).json()
    assert slip["monthly_ctc_minor"] == line["monthly_ctc_minor"]
    assert slip["breakdown"] == line["breakdown"]
    assert slip["prorated"] == line["prorated"]
    assert slip["net_minor"] == line["net_minor"]
    assert slip["payable_days"] == line["payable_days"]


async def test_my_slip_without_compensation_is_zero(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    # The outsider has no compensation row set -> an all-zero, flagged slip.
    resp = await client.get("/api/v1/payroll/me", headers=auth_headers(settings, seed.outsider))
    assert resp.status_code == 200
    slip = resp.json()
    assert slip["missing_compensation"] is True
    assert slip["monthly_ctc_minor"] == 0
    assert slip["net_minor"] == 0


async def test_my_slip_is_per_caller(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    """Two people hitting the same endpoint get their OWN slips, never shared."""
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    report_slip = (
        await client.get("/api/v1/payroll/me", headers=auth_headers(settings, seed.report))
    ).json()
    outsider_slip = (
        await client.get("/api/v1/payroll/me", headers=auth_headers(settings, seed.outsider))
    ).json()
    assert report_slip["monthly_ctc_minor"] == 50_000_00
    assert outsider_slip["monthly_ctc_minor"] == 0
