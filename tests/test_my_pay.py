"""My Pay — the self-service payslip endpoint (`GET /payroll/me`).

Every employee may read their OWN slip here, and only their own — the endpoint
never lets a plain employee pass another's id. The numbers must match that
person's line in the HR estimate exactly (shared computation, no drift).

Release gate: a plain employee sees their slip only once the month is RELEASED
(finalized). HR/Admin (and payroll-manager grant holders) keep the live preview
before release so they can check the numbers. The live PDF is gated the same way,
since it is generated through this endpoint.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_MONTH = "2026-06"
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


async def _setup(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    """Give the report compensation + configure payroll settings (admin actions)."""
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    await client.put(
        "/api/v1/payroll/settings", json=_SETTINGS, headers=auth_headers(settings, seed.admin)
    )


async def _release(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.post(
        f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 200, resp.text


async def test_my_slip_hidden_until_released(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Before release, the employee can't see their own slip (or generate its PDF),
    but HR/Admin can still preview it live."""
    await _setup(client, settings, seed)

    hidden = await client.get(
        f"/api/v1/payroll/me?month={_MONTH}", headers=auth_headers(settings, seed.report)
    )
    assert hidden.status_code == 404
    hidden_pdf = await client.get(
        f"/api/v1/payroll/me/pdf?month={_MONTH}", headers=auth_headers(settings, seed.report)
    )
    assert hidden_pdf.status_code == 404

    # HR/Admin preview the same month live before releasing it.
    preview = await client.get(
        f"/api/v1/payroll/me?month={_MONTH}&employee_id={seed.report.id}",
        headers=auth_headers(settings, seed.admin),
    )
    assert preview.status_code == 200


async def test_my_slip_visible_after_release(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _setup(client, settings, seed)
    await _release(client, settings, seed)

    resp = await client.get(
        f"/api/v1/payroll/me?month={_MONTH}", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 200
    slip = resp.json()
    assert slip["month"] == _MONTH
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
    await _setup(client, settings, seed)

    est = (
        await client.get(
            f"/api/v1/payroll/estimate?month={_MONTH}", headers=auth_headers(settings, seed.admin)
        )
    ).json()
    line = next(line for line in est["lines"] if line["employee_id"] == str(seed.report.id))

    await _release(client, settings, seed)
    slip = (
        await client.get(
            f"/api/v1/payroll/me?month={_MONTH}", headers=auth_headers(settings, seed.report)
        )
    ).json()
    assert slip["monthly_ctc_minor"] == line["monthly_ctc_minor"]
    assert slip["breakdown"] == line["breakdown"]
    assert slip["prorated"] == line["prorated"]
    assert slip["net_minor"] == line["net_minor"]
    assert slip["payable_days"] == line["payable_days"]


async def test_missing_compensation_slip_is_zero_for_hr_preview(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The outsider has no compensation row -> an all-zero, flagged slip. Only HR/
    Admin can reach it live (a person with no comp is skipped at release, so their
    own view stays gated)."""
    resp = await client.get(
        f"/api/v1/payroll/me?employee_id={seed.outsider.id}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    slip = resp.json()
    assert slip["missing_compensation"] is True
    assert slip["monthly_ctc_minor"] == 0
    assert slip["net_minor"] == 0


async def test_my_slip_is_per_caller(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    """A plain employee never reaches another person's pay: passing someone else's
    id is rejected, and the released self-view returns only their own numbers."""
    await _setup(client, settings, seed)
    await _release(client, settings, seed)

    # The report reads their own released slip.
    own = await client.get(
        f"/api/v1/payroll/me?month={_MONTH}", headers=auth_headers(settings, seed.report)
    )
    assert own.status_code == 200
    assert own.json()["monthly_ctc_minor"] == 50_000_00

    # A plain employee cannot pass another's id to peek at their pay.
    peek = await client.get(
        f"/api/v1/payroll/me?month={_MONTH}&employee_id={seed.report.id}",
        headers=auth_headers(settings, seed.outsider),
    )
    assert peek.status_code == 403
