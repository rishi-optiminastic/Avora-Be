"""Payroll adjustments — HR/Admin-only management + their effect on the estimate.

Proves the authz gate (CLAUDE §9) and that each adjustment kind flows into the
payroll line: earnings/deductions move net, a NET_PAY override forces it, and a
LOP_DAYS override replaces the attendance-derived loss-of-pay.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings
from tests.conftest import _Seed, auth_headers

_COMP = {"amount_minor": 50_000_00, "currency": "inr", "period": "monthly"}
_MONTH = "2026-06"


async def _set_comp(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    await client.put(
        f"/api/v1/employees/{seed.report.id}/compensation",
        json=_COMP,
        headers=auth_headers(settings, seed.admin),
    )


def _adj(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "employee_id": "",  # filled by caller
        "period_month": _MONTH,
        "kind": "earning",
        "label": "Diwali bonus",
        "amount_minor": 1_000_00,
    }
    body.update(over)
    return body


async def _report_line(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> dict[str, object]:
    resp = await client.get(
        f"/api/v1/payroll/estimate?month={_MONTH}", headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 200
    return next(
        line for line in resp.json()["lines"] if line["employee_id"] == str(seed.report.id)
    )


async def test_only_hr_admin_manages_adjustments(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = _adj(employee_id=str(seed.report.id))
    for actor in (seed.report, seed.manager, seed.outsider):
        denied = await client.post(
            "/api/v1/payroll/adjustments", json=body, headers=auth_headers(settings, actor)
        )
        assert denied.status_code == 403, f"{actor.work_email} must not add adjustments"
        listed = await client.get(
            f"/api/v1/payroll/adjustments?month={_MONTH}", headers=auth_headers(settings, actor)
        )
        assert listed.status_code == 403

    ok = await client.post(
        "/api/v1/payroll/adjustments", json=body, headers=auth_headers(settings, seed.admin)
    )
    assert ok.status_code == 201, ok.text
    adj_id = ok.json()["id"]
    # A non-manager can't delete it either.
    assert (
        await client.delete(
            f"/api/v1/payroll/adjustments/{adj_id}", headers=auth_headers(settings, seed.report)
        )
    ).status_code == 403
    gone = await client.delete(
        f"/api/v1/payroll/adjustments/{adj_id}", headers=auth_headers(settings, seed.admin)
    )
    assert gone.status_code == 204


async def test_validation_rejects_bad_shapes(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    h = auth_headers(settings, seed.admin)
    # OVERRIDE without a target.
    r1 = await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(employee_id=str(seed.report.id), kind="override", target=None),
        headers=h,
    )
    assert r1.status_code == 422
    # EARNING with a target.
    r2 = await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(employee_id=str(seed.report.id), kind="earning", target="basic"),
        headers=h,
    )
    assert r2.status_code == 422
    # EARNING of zero.
    r3 = await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(employee_id=str(seed.report.id), amount_minor=0),
        headers=h,
    )
    assert r3.status_code == 422


async def test_earning_and_deduction_move_net(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_comp(client, settings, seed)
    base = await _report_line(client, settings, seed)
    base_net = base["net_minor"]

    await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(employee_id=str(seed.report.id), kind="earning", amount_minor=1_000_00),
        headers=auth_headers(settings, seed.admin),
    )
    await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(
            employee_id=str(seed.report.id),
            kind="deduction",
            label="Advance recovery",
            amount_minor=300_00,
        ),
        headers=auth_headers(settings, seed.admin),
    )
    line = await _report_line(client, settings, seed)
    assert line["adjustment_earnings_minor"] == 1_000_00
    assert line["adjustment_deductions_minor"] == 300_00
    assert line["net_minor"] == base_net + 1_000_00 - 300_00


async def test_net_override_forces_net(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_comp(client, settings, seed)
    await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(
            employee_id=str(seed.report.id),
            kind="override",
            target="net_pay",
            label="Agreed net",
            amount_minor=42_000_00,
        ),
        headers=auth_headers(settings, seed.admin),
    )
    line = await _report_line(client, settings, seed)
    assert line["net_minor"] == 42_000_00


async def test_lop_override_replaces_days(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _set_comp(client, settings, seed)
    # Force loss-of-pay to 5.0 days (stored as days x 100).
    await client.post(
        "/api/v1/payroll/adjustments",
        json=_adj(
            employee_id=str(seed.report.id),
            kind="override",
            target="lop_days",
            label="Corrected LOP",
            amount_minor=5_00,
        ),
        headers=auth_headers(settings, seed.admin),
    )
    line = await _report_line(client, settings, seed)
    assert line["lop_days"] == 5.0
