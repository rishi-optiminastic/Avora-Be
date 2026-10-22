"""Proof that a month's payroll was actually paid.

The payroll run says what SHOULD have gone out; a receipt is the document
showing it did - the bank's advice, a UTR confirmation. Pinned here:

- only people who may RUN payroll may file or read the evidence (CLAUDE §9);
- several receipts per month are fine (salaries and expenses often go out as
  separate transfers) but the same FILE twice is not;
- the month reports whether it can be proved, without that ever blocking
  payroll itself;
- removing one is audited, because deleting evidence is the interesting event.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.audit import AuditLog
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

_MONTH = "2026-06"
_PDF = b"%PDF-1.4\nfake transfer advice\n%%EOF"


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-receipts",
        work_email="hr.receipts@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _finance(db: AsyncSession) -> Employee:
    """An ordinary executive carrying the payroll-manager grant - the finance
    person who actually makes the transfers."""
    person = Employee(
        hr_external_id="fin-receipts",
        work_email="finance.receipts@corp.test",
        full_name="Fin Ance",
        role=Role.EXECUTIVE,
        payroll_manager=True,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


async def _upload(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    *,
    data: bytes = _PDF,
    month: str = _MONTH,
    content_type: str = "application/pdf",
    extra: dict[str, str] | None = None,
):
    return await client.post(
        "/api/v1/payroll/receipts",
        data={"period_month": month, **(extra or {})},
        files={"file": ("advice.pdf", data, content_type)},
        headers=auth_headers(settings, who),
    )


# --------------------------------------------------------------------------- #
# Who may file and read it
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    for path in ("/api/v1/payroll/receipts", f"/api/v1/payroll/receipts?month={_MONTH}"):
        assert (await client.get(path)).status_code == 401


async def test_hr_can_file_a_receipt(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)

    resp = await _upload(
        client,
        settings,
        hr,
        extra={"reference": "UTR123456", "paid_on": "2026-07-01", "amount_minor": "250000000"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["period_month"] == _MONTH
    assert body["reference"] == "UTR123456"
    assert body["amount_minor"] == 250000000
    assert body["kind"] == "bank_advice"
    assert body["uploaded_by_name"] == "Hana HR"
    assert body["byte_size"] == len(_PDF)


async def test_a_payroll_manager_grant_is_enough(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The finance person who makes the transfer is usually not HR. If they
    cannot file the receipt, the person with the document is not the person
    with the button and it never gets filed."""
    finance = await _finance(db)
    assert (await _upload(client, settings, finance)).status_code == 201


async def test_ordinary_people_cannot_file_or_read_receipts(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    assert (await _upload(client, settings, hr)).status_code == 201

    for actor in (seed.report, seed.manager, seed.outsider):
        headers = auth_headers(settings, actor)
        assert (await _upload(client, settings, actor)).status_code == 403
        listed = await client.get(f"/api/v1/payroll/receipts?month={_MONTH}", headers=headers)
        assert listed.status_code == 403
        status = await client.get(
            f"/api/v1/payroll/receipts/status?month={_MONTH}", headers=headers
        )
        assert status.status_code == 403


# --------------------------------------------------------------------------- #
# What is accepted
# --------------------------------------------------------------------------- #
async def test_the_same_document_cannot_be_filed_twice(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A re-upload is a double-click, not a second payment."""
    hr = await _hr(db)
    assert (await _upload(client, settings, hr)).status_code == 201
    again = await _upload(client, settings, hr)
    assert again.status_code == 409


async def test_the_same_document_may_be_filed_against_a_different_month(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    assert (await _upload(client, settings, hr)).status_code == 201
    assert (await _upload(client, settings, hr, month="2026-07")).status_code == 201


async def test_several_transfers_in_one_month_are_fine(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Salaries and reimbursements often go out separately."""
    hr = await _hr(db)
    assert (await _upload(client, settings, hr, data=b"%PDF-salaries")).status_code == 201
    assert (await _upload(client, settings, hr, data=b"%PDF-expenses")).status_code == 201

    listed = await client.get(
        f"/api/v1/payroll/receipts?month={_MONTH}", headers=auth_headers(settings, hr)
    )
    assert len(listed.json()) == 2


async def test_an_unreal_month_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    assert (await _upload(client, settings, hr, month="2026-13")).status_code == 422


async def test_only_a_pdf_or_image_is_accepted(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    resp = await _upload(client, settings, hr, content_type="application/zip")
    assert resp.status_code == 422


async def test_an_empty_file_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    assert (await _upload(client, settings, hr, data=b"")).status_code == 422


# --------------------------------------------------------------------------- #
# Can the month be proved?
# --------------------------------------------------------------------------- #
async def test_a_month_with_no_receipt_reports_no_proof(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    resp = await client.get(
        f"/api/v1/payroll/receipts/status?month={_MONTH}", headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "period_month": _MONTH,
        "receipt_count": 0,
        "total_minor": 0,
        "currency": "INR",
        "latest_paid_on": None,
        "has_proof": False,
    }


async def test_proof_status_totals_only_the_amounts_given(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A receipt filed without an amount still counts as evidence; it just adds
    nothing to the total, which is "what the evidence accounts for" rather than
    a second opinion on payroll."""
    hr = await _hr(db)
    await _upload(
        client,
        settings,
        hr,
        data=b"%PDF-a",
        extra={"amount_minor": "100000", "paid_on": "2026-07-01"},
    )
    await _upload(client, settings, hr, data=b"%PDF-b", extra={"paid_on": "2026-07-03"})

    resp = await client.get(
        f"/api/v1/payroll/receipts/status?month={_MONTH}", headers=auth_headers(settings, hr)
    )

    body = resp.json()
    assert body["receipt_count"] == 2
    assert body["total_minor"] == 100000
    assert body["latest_paid_on"] == "2026-07-03"
    assert body["has_proof"] is True


async def test_a_missing_receipt_never_blocks_payroll(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Evidence is asked for, not enforced. Withholding payslips because a PDF
    has not been filed would punish employees for an admin omission."""
    hr = await _hr(db)
    resp = await client.get(
        f"/api/v1/payroll/estimate?month={_MONTH}", headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Download and removal
# --------------------------------------------------------------------------- #
async def test_the_document_comes_back_byte_for_byte(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    receipt_id = (await _upload(client, settings, hr)).json()["id"]

    resp = await client.get(
        f"/api/v1/payroll/receipts/{receipt_id}/file", headers=auth_headers(settings, hr)
    )

    assert resp.status_code == 200
    assert resp.content == _PDF
    assert resp.headers["content-type"] == "application/pdf"


async def test_nobody_outside_payroll_can_download_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    receipt_id = (await _upload(client, settings, hr)).json()["id"]
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(
            f"/api/v1/payroll/receipts/{receipt_id}/file", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 403


async def test_a_missing_receipt_is_a_404(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    resp = await client.get(
        f"/api/v1/payroll/receipts/{uuid.uuid4()}/file", headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 404


async def test_removing_one_is_audited(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    receipt_id = (await _upload(client, settings, hr)).json()["id"]

    resp = await client.delete(
        f"/api/v1/payroll/receipts/{receipt_id}", headers=auth_headers(settings, hr)
    )

    assert resp.status_code == 204
    listed = await client.get(
        f"/api/v1/payroll/receipts?month={_MONTH}", headers=auth_headers(settings, hr)
    )
    assert listed.json() == []
    rows = await db.execute(select(AuditLog).where(AuditLog.action == "payroll.receipt.remove"))
    assert rows.scalars().first() is not None


async def test_ordinary_people_cannot_remove_one(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _hr(db)
    receipt_id = (await _upload(client, settings, hr)).json()["id"]
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.delete(
            f"/api/v1/payroll/receipts/{receipt_id}", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 403
