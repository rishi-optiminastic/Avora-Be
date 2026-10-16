"""Payout verification end to end: upload, verify, flag HR, resolve.

Synthetic people and amounts only. A month is "released" by writing payslips
directly, which is what `/payroll/finalize` leaves behind.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.pii_crypto import encrypt_pii
from app.models import Employee, EmployeeStatus, Role
from app.models.audit import AuditLog
from app.models.compensation import Compensation
from app.models.notification import Notification
from app.models.payout_verification import PayoutUpload
from app.models.payslip import Payslip
from app.models.reimbursement import Reimbursement, ReimbursementStatus
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.notification import NotificationRepository
from app.repositories.payout_verification import PayoutVerificationRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.payslip import PayslipRepository
from app.repositories.reimbursement import ReimbursementRepository
from app.services.notification_service import NotificationService
from app.services.payout_verification_service import PayoutVerificationService
from tests import conftest
from tests.conftest import _Seed, auth_headers

MONTH = "2026-04"
URL = "/api/v1/payroll/payouts"


class _Org:
    def __init__(self, seed: _Seed, hr: Employee) -> None:
        self.seed = seed
        self.hr = hr


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every generic `send` (the fake email service drops them)."""
    sent: list[str] = []

    async def _send(self: object, *, to: str, subject: str, html: str) -> None:
        sent.append(to)

    monkeypatch.setattr(conftest._FakeEmailService, "send", _send)
    return sent


@pytest.fixture
async def org(db: AsyncSession, seed: _Seed, settings: Settings) -> _Org:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Hana HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    numbers = {seed.report: "7", seed.outsider: "8", seed.manager: "9", seed.admin: "1"}
    for person, number in numbers.items():
        person.employee_number = number
    db.add(
        Compensation(
            employee_id=seed.outsider.id,
            account_number_encrypted=encrypt_pii(settings, "000123456789"),
        )
    )
    # Released: report 50,000.00; outsider 40,000.40 (register says 40,000);
    # manager 30,500.00, of which 500 is a reimbursement folded into net pay (as
    # finalize stores it). The admin has no payslip.
    for person, net in ((seed.report, 5_000_000), (seed.outsider, 4_000_040)):
        db.add(_slip(person, net))
    db.add(_slip(seed.manager, 3_050_000, reimbursement=50_000))
    await db.commit()
    return _Org(seed, hr)


def _slip(person: Employee, net: int, *, reimbursement: int = 0) -> Payslip:
    return Payslip(
        employee_id=person.id,
        period_month=MONTH,
        employee_name=person.full_name,
        currency="INR",
        net_minor=net,
        reimbursement_minor=reimbursement,
    )


def _csv(*lines: str) -> dict[str, tuple[str, bytes, str]]:
    body = "\n".join(("Employee No,Employee Name,Account Number,Work Email,Net Pay,UTR", *lines))
    return {"file": ("payout.csv", body.encode(), "text/csv")}


_MIXED = _csv(
    "7,Report,,,50000,UTR1",  # exact
    ",Outsider,123456789,,41000,UTR2",  # by account, amount differs
    ",Admin,,admin@corp.test,100,UTR3",  # paid, nothing due
    ",Ghost,,,700,UTR4",  # nobody we know
)  # the manager (30,500 due) is missing: expected but not paid


async def _upload(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    files: dict[str, tuple[str, bytes, str]],
    kind: str = "salary",
) -> tuple[int, dict[str, object]]:
    res = await client.post(
        f"{URL}?month={MONTH}&kind={kind}", files=files, headers=auth_headers(settings, who)
    )
    return res.status_code, res.json()


def _counts(body: dict[str, object]) -> dict[str, int]:
    return {c["kind"]: c["total"] for c in body["counts"]}  # type: ignore[attr-defined]


async def _notified(db: AsyncSession, who: Employee) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.recipient_id == who.id, Notification.entity_type == "payout_upload")
    )
    return int(count or 0)


async def test_mixed_payout_flags_every_kind_and_hr_once(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    status, body = await _upload(client, settings, org.seed.admin, _MIXED)
    assert status == 201, body
    assert _counts(body) == {
        "paid_not_expected": 1,
        "expected_not_paid": 1,
        "amount_mismatch": 1,
        "duplicate_payment": 0,
        "unmatched_row": 1,
        "account_mismatch": 0,  # matched BY account, so it agrees by definition
    }
    assert body["warnings"] == []
    upload = body["upload"]
    assert upload["row_count"] == 4 and upload["matched_count"] == 3  # type: ignore[index]
    assert upload["expected_total_minor"] == 5_000_000 + 4_000_000 + 3_050_000  # type: ignore[index]
    mismatches = {m["kind"]: m for m in body["mismatches"]}  # type: ignore[attr-defined]
    short = mismatches["amount_mismatch"]
    assert short["employee_id"] == str(org.seed.outsider.id)
    assert (short["expected_minor"], short["paid_minor"]) == (4_000_000, 4_100_000)
    assert short["difference_minor"] == 100_000
    assert short["account_last4"] == "6789" and short["reference"] == "UTR2"
    assert mismatches["expected_not_paid"]["expected_minor"] == 3_050_000
    assert mismatches["unmatched_row"]["payee_name"] == "Ghost"

    # HR (and admins/payroll managers, never the uploader) are flagged once.
    assert await _notified(db, org.hr) == 1
    assert await _notified(db, org.seed.admin) == 0  # never your own upload
    assert "hr@corp.test" in mailbox and "admin@corp.test" not in mailbox
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "payroll.payout.upload" in actions


async def test_salary_only_and_reimbursement_runs_expect_their_own_amounts(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    db.add(
        Reimbursement(
            employee_id=org.seed.report.id,
            amount_minor=12_345,
            description="synthetic",
            expense_date=date(2026, 4, 3),
            period_month=MONTH,
            status=ReimbursementStatus.APPROVED,
        )
    )
    await db.commit()
    status, body = await _upload(
        client, settings, org.seed.admin, _csv("9,M,,,30000,U"), kind="salary_only"
    )
    assert status == 201
    assert not any(m["employee_id"] == str(org.seed.manager.id) for m in body["mismatches"])  # type: ignore[attr-defined]

    status, body = await _upload(
        client, settings, org.seed.admin, _csv("7,R,,,123,U"), kind="reimbursements"
    )
    assert status == 201
    assert body["upload"]["expected_total_minor"] == 12_300  # type: ignore[index]
    assert body["upload"]["mismatch_count"] == 0  # type: ignore[index]


async def test_duplicate_and_failed_lines(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    files = {
        "file": (
            "bank.csv",
            b"Emp ID,Amount,Status\n7,50000,Success\n7,50000,Success\n"
            b"8,40000,Success\n9,30500,Failed\n",
            "text/csv",
        )
    }
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 201
    counts = _counts(body)
    assert counts["duplicate_payment"] == 1
    assert counts["expected_not_paid"] == 1  # the failed transfer did not pay anyone
    assert counts["amount_mismatch"] == 0
    assert body["upload"]["failed_count"] == 1  # type: ignore[index]


async def test_clean_payout_notifies_nobody(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    files = _csv("7,,,,50000,", "8,,,,40000,", "9,,,,30500,")
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 201 and body["upload"]["mismatch_count"] == 0  # type: ignore[index]
    assert await _notified(db, org.hr) == 0
    assert mailbox == []


async def test_failed_email_never_undoes_the_check(
    client: AsyncClient,
    settings: Settings,
    db: AsyncSession,
    org: _Org,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(self: object, *, to: str, subject: str, html: str) -> None:
        raise RuntimeError("provider down")

    monkeypatch.setattr(conftest._FakeEmailService, "send", _boom)
    status, body = await _upload(client, settings, org.seed.admin, _MIXED)
    assert status == 201, body
    assert await db.scalar(select(func.count()).select_from(PayoutUpload)) == 1
    assert await _notified(db, org.hr) == 1


async def test_flag_to_hr_runs_once_per_upload(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    status, body = await _upload(client, settings, org.seed.admin, _MIXED)
    assert status == 201
    upload = await db.get(PayoutUpload, uuid.UUID(body["upload"]["id"]))  # type: ignore[index]
    assert upload is not None and upload.notified_at is not None
    service = PayoutVerificationService(
        PayoutVerificationRepository(db),
        PayslipRepository(db),
        ReimbursementRepository(db),
        CompensationRepository(db),
        EmployeeRepository(db),
        PayrollSettingsRepository(db),
        NotificationService(NotificationRepository(db)),
        conftest._FakeEmailService(),  # type: ignore[arg-type]
        AuditRepository(db),
        settings,
    )
    assert await service.flag_to_hr(upload, actor_id=None) is False
    assert await _notified(db, org.hr) == 1


async def test_review_and_resolve(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    _, body = await _upload(client, settings, org.seed.admin, _MIXED)
    upload_id = body["upload"]["id"]  # type: ignore[index]
    hr = auth_headers(settings, org.hr)

    latest = await client.get(f"{URL}/latest?month={MONTH}", headers=hr)
    assert latest.status_code == 200 and latest.json()["upload"]["id"] == upload_id
    listed = await client.get(f"{URL}?month={MONTH}", headers=hr)
    assert listed.status_code == 200 and listed.json()["total"] == 1

    mismatch_id = latest.json()["mismatches"][0]["id"]
    resolve_url = f"{URL}/{upload_id}/mismatches/{mismatch_id}/resolve"
    assert (await client.post(resolve_url, json={"note": "   "}, headers=hr)).status_code == 422
    done = await client.post(resolve_url, json={"note": "Paid by cheque"}, headers=hr)
    assert done.status_code == 200, done.text
    assert done.json()["resolution_note"] == "Paid by cheque"
    assert done.json()["resolved_by_name"] == "Hana HR"
    again = await client.post(resolve_url, json={"note": "again"}, headers=hr)
    assert again.status_code == 409

    detail = (await client.get(f"{URL}/{upload_id}", headers=hr)).json()
    assert sum(c["open"] for c in detail["counts"]) == 3
    wrong = f"{URL}/{uuid.uuid4()}/mismatches/{mismatch_id}/resolve"
    assert (await client.post(wrong, json={"note": "x"}, headers=hr)).status_code == 404
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert {"payroll.payout.resolve", "payroll.payout.view"} <= set(actions)
    assert await _notified(db, org.hr) == 1  # resolving never re-flags


async def test_unreleased_month_and_bad_files_are_refused(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    admin = auth_headers(settings, org.seed.admin)
    files = _csv("7,,,,1,")
    unreleased = await client.post(f"{URL}?month=2026-05", files=files, headers=admin)
    assert unreleased.status_code == 422
    assert "not been released" in unreleased.text
    bad_month = await client.post(f"{URL}?month=2026-13", files=files, headers=admin)
    assert bad_month.status_code == 422
    pdf = {"file": ("payout.csv", b"%PDF-1.7\x00\x00", "text/csv")}
    assert (await client.post(f"{URL}?month={MONTH}", files=pdf, headers=admin)).status_code == 422


async def test_only_payroll_managers_may_touch_payouts(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    _, body = await _upload(client, settings, org.seed.admin, _MIXED)
    upload_id = body["upload"]["id"]  # type: ignore[index]
    mismatch_id = body["mismatches"][0]["id"]  # type: ignore[index]
    for outsider in (org.seed.manager, org.seed.report):
        headers = auth_headers(settings, outsider)
        upload = await client.post(f"{URL}?month={MONTH}", files=_MIXED, headers=headers)
        assert upload.status_code == 403
        assert (await client.get(URL, headers=headers)).status_code == 403
        assert (await client.get(f"{URL}/latest", headers=headers)).status_code == 403
        assert (await client.get(f"{URL}/{upload_id}", headers=headers)).status_code == 403
        resolve = await client.post(
            f"{URL}/{upload_id}/mismatches/{mismatch_id}/resolve",
            json={"note": "x"},
            headers=headers,
        )
        assert resolve.status_code == 403

    org.seed.report.payroll_manager = True  # the finance grant opens it
    db.add(org.seed.report)
    await db.commit()
    granted = await _upload(client, settings, org.seed.report, _csv("7,R,,,50000,UTR9"))
    assert granted[0] == 201


async def test_same_file_twice_is_refused_and_alerts_once(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    first, _ = await _upload(client, settings, org.seed.admin, _MIXED)
    assert first == 201
    again, body = await _upload(client, settings, org.seed.admin, _MIXED)
    assert again == 409
    assert "already checked" in str(body)
    assert await db.scalar(select(func.count()).select_from(PayoutUpload)) == 1
    assert await _notified(db, org.hr) == 1 and mailbox.count("hr@corp.test") == 1
    # The same bytes for another run kind are a different check.
    other, _ = await _upload(client, settings, org.seed.admin, _MIXED, kind="salary_only")
    assert other == 201


async def test_zero_rows_and_negative_net_pay(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    files = _csv(
        "7,Report,,,-1200,",  # due 50,000 but the row went negative: not paid
        "8,,,,40000,",
        "9,,,,30500,",
        "1,Admin,,,0,",  # Avora's register lists everyone; nothing due, nothing paid
        ",Nobody,,,0,",  # an unknown zero row moved no money either
    )
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 201, body
    counts = _counts(body)
    assert counts["expected_not_paid"] == 1
    assert counts["paid_not_expected"] == 0 and counts["unmatched_row"] == 0
    (unpaid,) = body["mismatches"]  # type: ignore[misc]
    assert unpaid["employee_id"] == str(org.seed.report.id)
    assert body["upload"]["paid_total_minor"] == 4_000_000 + 3_050_000  # type: ignore[index]


async def test_payment_to_a_different_account_is_flagged(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    files = _csv("7,,,,50000,", "8,Outsider,555566667777,,40000,UTR8", "9,,,,30500,")
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 201, body
    (moved,) = body["mismatches"]  # type: ignore[misc]
    assert moved["kind"] == "account_mismatch"
    assert moved["employee_id"] == str(org.seed.outsider.id)
    assert moved["account_last4"] == "7777" and moved["account_on_file_last4"] == "6789"
    # Only last fours ever leave the server.
    assert "123456789" not in str(body) and "555566667777" not in str(body)
    assert "hr@corp.test" in mailbox

    leading_zeros = _csv("7,,,,50000,", "8,,0000123456789,,40000,", "9,,,,30500,")
    status, body = await _upload(client, settings, org.seed.admin, leading_zeros)
    assert status == 201 and body["upload"]["mismatch_count"] == 0  # type: ignore[index]


async def test_reimbursement_run_expects_the_export_roster_only(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    org.seed.outsider.is_active = False  # a leaver with an approved claim
    db.add(org.seed.outsider)
    for person in (org.seed.report, org.seed.outsider):
        db.add(
            Reimbursement(
                employee_id=person.id,
                amount_minor=50_000,
                description="synthetic",
                expense_date=date(2026, 4, 3),
                period_month=MONTH,
                status=ReimbursementStatus.APPROVED,
            )
        )
    await db.commit()
    status, body = await _upload(
        client, settings, org.seed.admin, _csv("7,R,,,500,U"), kind="reimbursements"
    )
    assert status == 201, body
    assert body["upload"]["expected_count"] == 1  # type: ignore[index]
    assert body["upload"]["mismatch_count"] == 0  # type: ignore[index]


async def test_hr_is_mailed_only_after_the_check_is_committed(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch, org: _Org
) -> None:
    events: list[str] = []
    real_commit = PayoutVerificationRepository.commit

    async def _commit(self: PayoutVerificationRepository) -> None:
        await real_commit(self)
        events.append("commit")

    async def _send(self: object, *, to: str, subject: str, html: str) -> None:
        events.append("send")

    monkeypatch.setattr(PayoutVerificationRepository, "commit", _commit)
    monkeypatch.setattr(conftest._FakeEmailService, "send", _send)
    status, _ = await _upload(client, settings, org.seed.admin, _MIXED)
    assert status == 201
    assert "send" in events
    assert events.index("commit") < events.index("send")
    # Upload committed, then the in-app flags, then mail.
    assert events[:2] == ["commit", "commit"]


async def test_review_link_opens_the_payout_tab(
    client: AsyncClient, settings: Settings, db: AsyncSession, org: _Org, mailbox: list[str]
) -> None:
    await _upload(client, settings, org.seed.admin, _MIXED)
    link = await db.scalar(select(Notification.link).where(Notification.recipient_id == org.hr.id))
    assert link == "/dashboard/admin/settings?tab=payroll&view=payout"


def _xlsx(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


async def test_rounded_account_numbers_are_reported_not_matched(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    data = _xlsx(
        [
            ["Employee No", "Account Number", "Net Pay"],
            [7, 1234567890123456789.0, 50000],
            [8, 123456789.0, 40000],
            [9, None, 30500],
        ]
    )
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    files = {"file": ("payout.xlsx", data, xlsx)}
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 201, body
    assert body["upload"]["mismatch_count"] == 0  # type: ignore[index]
    (warning,) = body["warnings"]  # type: ignore[misc]
    assert "Row 2" in warning and "as text in Excel" in warning


async def test_unreadable_cell_is_a_422(
    client: AsyncClient, settings: Settings, org: _Org, mailbox: list[str]
) -> None:
    files = _csv("7,,,,50000,", "8,,,,Rs. 1e9999,")
    status, body = await _upload(client, settings, org.seed.admin, files)
    assert status == 422
    assert "Row 3" in str(body)
