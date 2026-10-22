"""Who may read what Avora mirrors from Circle, and that the fields Circle owns
cannot be edited in Avora once the person is in Circle.

Reads (circle-profile, appraisals, assets): the person, HR/Admin and payroll
only - a manager gets 403 even for their own report. Writes: pay, bank and work
location of a Circle-linked person are refused with "Edit this in Circle".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.pii_crypto import encrypt_pii
from app.models.circle_record import EmployeeAppraisal, EmployeeAsset, EmployeeCircleProfile
from app.models.compensation import Compensation, PayPeriod
from app.models.employee import Employee
from tests.conftest import _Seed, auth_headers

_READS = ("circle-profile", "appraisals", "assets")
_PAY = {
    "amount_minor": 60_000_000,
    "bonus_minor": 0,
    "currency": "INR",
    "period": "annual",
    "pf_enabled": True,
}
_BANK = {"bank_name": "HDFC", "account_number": "123456789", "ifsc_code": "HDFC0001234"}


async def _link(db: AsyncSession, settings: Settings, employee: Employee) -> None:
    """What the Circle sync leaves behind for a person Circle knows."""
    row = await db.get(Employee, employee.id)
    assert row is not None
    row.circle_linked_at = datetime.now(UTC)
    row.employee_number = "EMP-1001"
    row.location = "Mumbai"
    db.add_all(
        [
            Compensation(
                employee_id=employee.id,
                amount_minor=_PAY["amount_minor"],
                currency="INR",
                period=PayPeriod.ANNUAL,
                pf_enabled=True,
                bank_name=_BANK["bank_name"],
                account_number_encrypted=encrypt_pii(settings, _BANK["account_number"]),
                ifsc_code=_BANK["ifsc_code"],
            ),
            EmployeeCircleProfile(
                employee_id=employee.id,
                phone_encrypted=encrypt_pii(settings, "+91 98765 43210"),
                pan_masked="XXXXXX234F",
                aadhaar_masked="XXXX XXXX 9012",
                employment_type="Full-time",
            ),
            EmployeeAppraisal(
                employee_id=employee.id,
                circle_id="AP-1",
                period="Annual 2025",
                status="Completed",
                current_salary="6 LPA",
                revised_salary="7 LPA",
                effective_date=date(2025, 4, 1),
                score=4.0,
            ),
            EmployeeAsset(employee_id=employee.id, circle_id="AST-1", name="MacBook"),
        ]
    )
    await db.commit()


def _url(employee: Employee, path: str) -> str:
    return f"/api/v1/employees/{employee.id}/{path}"


# --- reads -------------------------------------------------------------------------


@pytest.mark.parametrize("path", _READS)
async def test_person_and_admin_can_read(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, path: str
) -> None:
    await _link(db, settings, seed.report)
    for caller in (seed.report, seed.admin):
        resp = await client.get(_url(seed.report, path), headers=auth_headers(settings, caller))
        assert resp.status_code == 200, caller.work_email


@pytest.mark.parametrize("path", _READS)
async def test_manager_and_outsider_are_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, path: str
) -> None:
    await _link(db, settings, seed.report)
    # The manager leads this report, and still may not see these.
    for caller in (seed.manager, seed.outsider):
        resp = await client.get(_url(seed.report, path), headers=auth_headers(settings, caller))
        assert resp.status_code == 403, caller.work_email


@pytest.mark.parametrize("path", _READS)
async def test_unknown_employee_is_404(
    client: AsyncClient, settings: Settings, seed: _Seed, path: str
) -> None:
    resp = await client.get(
        f"/api/v1/employees/00000000-0000-0000-0000-000000000000/{path}",
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 404


async def test_profile_read_shows_masked_ids_and_decrypted_contact(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _link(db, settings, seed.report)
    body: dict[str, Any] = (
        await client.get(
            _url(seed.report, "circle-profile"), headers=auth_headers(settings, seed.report)
        )
    ).json()
    assert body["linked"] is True
    assert body["circle_url"] == "https://circle.optiminastic.com/employees/EMP-1001"
    assert (body["pan_masked"], body["aadhaar_masked"]) == ("XXXXXX234F", "XXXX XXXX 9012")
    assert body["phone"] == "+91 98765 43210" and body["work_location"] == "Mumbai"


async def test_appraisal_read_shape(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _link(db, settings, seed.report)
    [row] = (
        await client.get(
            _url(seed.report, "appraisals"), headers=auth_headers(settings, seed.admin)
        )
    ).json()
    assert row == {
        "circle_id": "AP-1",
        "period": "Annual 2025",
        "status": "Completed",
        "current_salary": "6 LPA",
        "revised_salary": "7 LPA",
        "effective_date": "2025-04-01",
        "score": 4.0,
    }


async def test_unlinked_person_reads_as_not_linked(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    body = (
        await client.get(
            _url(seed.report, "circle-profile"), headers=auth_headers(settings, seed.admin)
        )
    ).json()
    assert body["linked"] is False and body["circle_url"] is None


# --- Circle-owned fields cannot be edited here ---------------------------------------


async def test_pay_change_is_refused_for_a_linked_person(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _link(db, settings, seed.report)
    admin = auth_headers(settings, seed.admin)
    resp = await client.put(
        _url(seed.report, "compensation"), json={**_PAY, "amount_minor": 1}, headers=admin
    )
    assert resp.status_code == 409 and "Edit this in Circle" in resp.text
    # Avora's own fields still save when Circle's are sent back unchanged.
    ok = await client.put(
        _url(seed.report, "compensation"),
        json={**_PAY, "bonus_minor": 7_000, "note": "Avora note"},
        headers=admin,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["bonus_minor"] == 7_000 and body["circle_managed"] is True
    assert body["circle_url"].endswith("/employees/EMP-1001")


@pytest.mark.parametrize("who", ["admin", "report"])
async def test_bank_change_is_refused_for_a_linked_person(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession, who: str
) -> None:
    await _link(db, settings, seed.report)
    headers = auth_headers(settings, getattr(seed, who))
    resp = await client.put(
        _url(seed.report, "compensation/bank"),
        json={**_BANK, "account_number": "987654321"},
        headers=headers,
    )
    assert resp.status_code == 409 and "Edit this in Circle" in resp.text
    # Holder name and account type stay Avora's.
    ok = await client.put(
        _url(seed.report, "compensation/bank"),
        json={**_BANK, "account_holder_name": "Remy Report", "account_type": "savings"},
        headers=headers,
    )
    assert ok.status_code == 200


async def test_unlinked_person_is_edited_in_avora_as_before(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    admin = auth_headers(settings, seed.admin)
    resp = await client.put(_url(seed.report, "compensation"), json=_PAY, headers=admin)
    assert resp.status_code == 200 and resp.json()["circle_managed"] is False
    bank = await client.put(_url(seed.report, "compensation/bank"), json=_BANK, headers=admin)
    assert bank.status_code == 200


async def test_work_location_change_is_refused_for_a_linked_person(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _link(db, settings, seed.report)
    admin = auth_headers(settings, seed.admin)
    url = f"/api/v1/employees/{seed.report.id}"
    resp = await client.patch(
        url, json={"full_name": "Remy Report", "location": "Delhi"}, headers=admin
    )
    assert resp.status_code == 409 and "Edit this in Circle" in resp.text
    same = await client.patch(
        url, json={"full_name": "Remy R.", "location": "Mumbai"}, headers=admin
    )
    assert same.status_code == 200 and same.json()["circle_linked"] is True
