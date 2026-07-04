"""Payslips — HR finalize/release, self-service download, and need-to-know authz.

Covers the new extensive-payroll surface: HR freezes a month into per-employee
snapshots, every employee can list + download their OWN released payslip (PDF),
HR/Admin can fetch anyone's, and no one else can (CLAUDE §9). The PDF renderer is
also smoke-tested as a pure function.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from app.core.payslip_pdf import PayslipPdfData, render_payslip_pdf
from tests.conftest import _Seed, auth_headers

_COMP = {"amount_minor": 50_000_00, "currency": "inr", "period": "monthly"}
_MONTH = "2026-06"


def test_render_payslip_pdf_produces_a_pdf() -> None:
    data = PayslipPdfData(
        org_name="Optiminastic",
        employee_name="Rishi Patel",
        department="Engineering",
        month_label="June 2026",
        currency="INR",
        monthly_ctc_minor=50_000_00,
        basic_minor=15_000_00,
        hra_minor=7_500_00,
        special_allowance_minor=25_700_00,
        gross_minor=48_200_00,
        employee_pf_minor=1_800_00,
        professional_tax_minor=200_00,
        income_tax_minor=0,  # no TDS on the reference ₹50k slip (deductions = PF + PT)
        total_deduction_minor=2_000_00,
        net_minor=46_200_00,
        net_payable_minor=42_000_00,
        working_days=22,
        present_days=20.0,
        paid_leave_days=0.0,
        payable_days=20.0,
        generated_label="30 Jun 2026",
    )
    pdf = render_payslip_pdf(data)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 800  # a real, non-empty document


async def _give_comp(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    resp = await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code in (200, 201)


# ---- finalize releases, then the employee can self-serve ------------------- #


async def test_finalize_releases_and_employee_downloads_own_payslip(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _give_comp(client, settings, seed)

    finalize = await client.post(
        f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    assert finalize.status_code == 200
    result = finalize.json()
    assert result["month"] == _MONTH
    assert result["released_count"] >= 1
    # The outsider has no compensation, so they are skipped, not released.
    assert result["skipped_count"] >= 1

    # The employee can now list their released history…
    listed = await client.get(
        "/api/v1/payroll/payslips", headers=auth_headers(settings, seed.report)
    )
    assert listed.status_code == 200
    assert any(p["month"] == _MONTH for p in listed.json())

    # …read the released detail (frozen snapshot)…
    detail = await client.get(
        f"/api/v1/payroll/payslips/{_MONTH}", headers=auth_headers(settings, seed.report)
    )
    assert detail.status_code == 200
    assert detail.json()["employee_id"] == str(seed.report.id)

    # …and download the PDF.
    pdf = await client.get(
        f"/api/v1/payroll/payslips/{_MONTH}/pdf", headers=auth_headers(settings, seed.report)
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


async def test_unfinalized_month_is_not_visible_to_the_employee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _give_comp(client, settings, seed)
    # Released June, but ask for May — nothing released → 404, not a live estimate.
    await client.post(
        f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    resp = await client.get(
        "/api/v1/payroll/payslips/2026-05", headers=auth_headers(settings, seed.report)
    )
    assert resp.status_code == 404


# ---- authorization: pay is need-to-know ------------------------------------ #


async def test_finalize_forbidden_for_non_hr_admin(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 403, f"{actor.work_email} must not finalize payroll"


async def test_employee_cannot_reach_another_persons_payslip(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _give_comp(client, settings, seed)
    await client.post(
        f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    target = f"employee_id={seed.report.id}"
    # The manager is NOT HR/Admin and the report is not "self" → 403 (no manager
    # carve-out for pay, even for a direct report).
    for path in (
        f"/api/v1/payroll/payslips?{target}",
        f"/api/v1/payroll/payslips/{_MONTH}?{target}",
        f"/api/v1/payroll/payslips/{_MONTH}/pdf?{target}",
    ):
        resp = await client.get(path, headers=auth_headers(settings, seed.manager))
        assert resp.status_code == 403, f"{path} should be forbidden to the manager"


async def test_hr_admin_can_download_any_employees_payslip(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _give_comp(client, settings, seed)
    await client.post(
        f"/api/v1/payroll/finalize?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    resp = await client.get(
        f"/api/v1/payroll/payslips/{_MONTH}/pdf?employee_id={seed.report.id}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
