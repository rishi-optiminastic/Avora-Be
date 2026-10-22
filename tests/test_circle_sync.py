"""The Circle sync: Circle wins for what it owns, an empty value never wipes
Avora's, ID numbers are only ever stored masked, and every change is audited
by name, never by value.

Runs the real scheduler steps against a fake Circle (see test_circle_import).
"""

# The `scheduler` fixture is imported from test_circle_import and then named as
# a test argument, which ruff reads as a redefinition.
# ruff: noqa: F811

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.pii_crypto import decrypt_pii, encrypt_pii
from app.models.audit import AuditLog
from app.models.circle_record import EmployeeAppraisal, EmployeeAsset, EmployeeCircleProfile
from app.models.compensation import Compensation, PayPeriod
from app.services.circle_values import mask_id_number
from tests.conftest import _Seed
from tests.test_circle_import import FakeCircle, _reload, scheduler  # noqa: F401 (a fixture)

Scheduler = tuple[Any, FakeCircle]

_FULL_PAN = "ABCDE1234F"
_FULL_AADHAAR = "123456789012"
_FULL_UAN = "100200300400"
_ACCOUNT = "123456789"


def _circle_pay(**overrides: Any) -> dict[str, Any]:
    pay: dict[str, Any] = {
        "employee_code": "EMP-1001",
        "annual_ctc_text": "6 LPA",
        "annual_ctc_inr": 600_000,
        "pf_enabled": False,
        "joining_date": "2026-08-21",
        "bank": {"bank_name": "HDFC", "account_number": _ACCOUNT, "ifsc_code": "HDFC0001234"},
    }
    pay.update(overrides)
    return pay


async def _comp(db: AsyncSession, employee_id: uuid.UUID) -> Compensation | None:
    row: Compensation | None = await db.scalar(
        select(Compensation)
        .where(Compensation.employee_id == employee_id)
        .execution_options(populate_existing=True)
    )
    return row


async def _seed_pay(db: AsyncSession, settings: Settings, employee_id: uuid.UUID) -> None:
    db.add(
        Compensation(
            employee_id=employee_id,
            amount_minor=100,
            bonus_minor=500,
            currency="USD",
            period=PayPeriod.MONTHLY,
            pf_enabled=True,
            note="kept by Avora",
            bank_name="Old Bank",
            account_number_encrypted=encrypt_pii(settings, "999999999"),
            ifsc_code="SBIN0000001",
        )
    )
    await db.commit()


async def _audits(db: AsyncSession, action: str) -> list[AuditLog]:
    return list((await db.scalars(select(AuditLog).where(AuditLog.action == action))).all())


# --- pay and bank ----------------------------------------------------------------


async def test_circle_wins_for_pay_and_bank(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession, settings: Settings
) -> None:
    module, fake = scheduler
    await _seed_pay(db, settings, seed.report.id)
    fake.compensation = _circle_pay()
    changed = await module.sync_records_for(seed.report.id)
    assert set(changed) >= {"amount_minor", "currency", "period", "pf_enabled", "account_number"}
    comp = await _comp(db, seed.report.id)
    assert comp is not None
    assert (comp.amount_minor, comp.currency, comp.period) == (60_000_000, "INR", PayPeriod.ANNUAL)
    assert comp.pf_enabled is False
    assert (comp.bank_name, comp.ifsc_code) == ("HDFC", "HDFC0001234")
    assert comp.account_number_encrypted and comp.account_number_encrypted != _ACCOUNT
    assert decrypt_pii(settings, comp.account_number_encrypted) == _ACCOUNT
    # Avora's own fields are untouched.
    assert (comp.bonus_minor, comp.note) == (500, "kept by Avora")
    [audit] = await _audits(db, "compensation.circle_sync")
    assert audit.actor == "circle-sync"
    assert _ACCOUNT not in (audit.target or "") and "600000" not in (audit.target or "")
    # Nothing new in Circle: nothing changes, nothing more audited.
    assert await module.sync_records_for(seed.report.id) == []
    assert len(await _audits(db, "compensation.circle_sync")) == 1


async def test_empty_or_invalid_circle_values_never_wipe(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession, settings: Settings
) -> None:
    module, fake = scheduler
    await _seed_pay(db, settings, seed.report.id)
    fake.compensation = _circle_pay(
        annual_ctc_inr=None,
        pf_enabled=None,
        bank={"bank_name": "", "account_number": None, "ifsc_code": "not-an-ifsc"},
    )
    assert await module.sync_records_for(seed.report.id) == []
    comp = await _comp(db, seed.report.id)
    assert comp is not None and comp.amount_minor == 100 and comp.currency == "USD"
    assert comp.bank_name == "Old Bank" and comp.ifsc_code == "SBIN0000001"
    assert decrypt_pii(settings, comp.account_number_encrypted or "") == "999999999"


async def test_new_record_gets_circles_joining_date(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.compensation = _circle_pay()
    await module.sync_records_for(seed.report.id)
    comp = await _comp(db, seed.report.id)
    assert comp is not None and str(comp.effective_date) == "2026-08-21"
    assert comp.updated_by is None  # the sync, not a person


async def test_person_unknown_to_circle_is_left_alone(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession
) -> None:
    module, _ = scheduler
    assert await module.sync_records_for(seed.outsider.id) == []
    assert await module.fill_profile_for(seed.outsider.id) == []
    assert await _comp(db, seed.outsider.id) is None
    assert (await _reload(db, seed.outsider.id)).circle_linked_at is None


# --- personal and employment details ----------------------------------------------


def _full_profile(fake: FakeCircle) -> None:
    fake.profile.update(
        {
            "employee_code": "EMP-1001",
            "work_location": "Mumbai",
            "employment_type": "Full-time",
            "phone": "+91 98765 43210",
            "personal_email": "remy@home.test",
            "address": "12 MG Road, Pune",
            "emergency_contact": "Sam 99999 00000",
            # A Circle regression sending full numbers must still never be stored.
            "pan_masked": _FULL_PAN,
            "aadhaar_masked": _FULL_AADHAAR,
            "uan_masked": _FULL_UAN,
        }
    )


async def _profile(db: AsyncSession, employee_id: uuid.UUID) -> EmployeeCircleProfile:
    row = await db.scalar(
        select(EmployeeCircleProfile)
        .where(EmployeeCircleProfile.employee_id == employee_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def test_details_mirror_circle_and_ids_are_masked(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession, settings: Settings
) -> None:
    module, fake = scheduler
    _full_profile(fake)
    changed = await module.fill_profile_for(seed.report.id)
    assert {"circle_link", "employee_number", "location", "phone", "pan_masked"} <= set(changed)
    report = await _reload(db, seed.report.id)
    assert report.circle_linked_at is not None
    assert (report.employee_number, report.location) == ("EMP-1001", "Mumbai")
    row = await _profile(db, seed.report.id)
    assert (row.pan_masked, row.aadhaar_masked, row.uan_masked) == (
        "XXXXXX234F",
        "XXXX XXXX 9012",
        "XXXXXXXX0400",
    )
    assert row.phone_encrypted and "98765" not in row.phone_encrypted
    assert decrypt_pii(settings, row.phone_encrypted) == "+91 98765 43210"
    assert row.employment_type == "Full-time"
    targets = " ".join(a.target or "" for a in await _audits(db, "profile.circle_sync"))
    for value in (_FULL_PAN, "XXXXXX234F", "98765", "Mumbai", "EMP-1001"):
        assert value not in targets


async def test_details_circle_lacks_are_kept(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession, settings: Settings
) -> None:
    module, fake = scheduler
    _full_profile(fake)
    await module.fill_profile_for(seed.report.id)
    for key in ("employee_code", "work_location", "phone", "pan_masked", "employment_type"):
        fake.profile[key] = None
    fake.profile["uan_masked"] = "XXXXXXXXXXXX"  # nothing readable: not a value
    await module.fill_profile_for(seed.report.id)
    report = await _reload(db, seed.report.id)
    assert (report.employee_number, report.location) == ("EMP-1001", "Mumbai")
    row = await _profile(db, seed.report.id)
    assert decrypt_pii(settings, row.phone_encrypted or "") == "+91 98765 43210"
    assert (row.pan_masked, row.uan_masked, row.employment_type) == (
        "XXXXXX234F",
        "XXXXXXXX0400",
        "Full-time",
    )


async def test_manager_follows_circle_but_an_unmatched_one_is_never_cleared(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.profile.update({"manager_code": "EMP-2", "manager_email": "Outsider@Corp.Test"})
    assert "manager_id" in await module.fill_profile_for(seed.report.id)
    assert (await _reload(db, seed.report.id)).manager_id == seed.outsider.id
    # A manager Circle names by code that Avora cannot match: logged, kept.
    for email in (None, "ghost@corp.test", "report@corp.test"):
        fake.profile.update({"manager_code": "EMP-404", "manager_email": email})
        assert "manager_id" not in await module.fill_profile_for(seed.report.id)
        assert (await _reload(db, seed.report.id)).manager_id == seed.outsider.id


def test_mask_id_number() -> None:
    assert mask_id_number(_FULL_PAN) == "XXXXXX234F"
    assert mask_id_number("1234 5678 9012", grouped=True) == "XXXX XXXX 9012"
    assert mask_id_number("XXXX XXXX 9012", grouped=True) == "XXXX XXXX 9012"  # idempotent
    assert mask_id_number("XXXXXXXX0400") == "XXXXXXXX0400"
    assert mask_id_number("1234") is None  # too short: would reveal itself
    assert mask_id_number("XXXXXXXXXXXX") is None
    assert mask_id_number(None) is None


# --- appraisal history and assets --------------------------------------------------


def _appraisal(circle_id: str, status: str, effective: str) -> dict[str, Any]:
    return {
        "id": circle_id,
        "period": "Annual 2025",
        "status": status,
        "current_salary": "6 LPA",
        "revised_salary": "7 LPA",
        "effective_date": effective,
        "score": 4,
    }


async def _rows(db: AsyncSession, model: Any, employee_id: uuid.UUID) -> dict[str, Any]:
    rows = await db.scalars(
        select(model)
        .where(model.employee_id == employee_id)
        .execution_options(populate_existing=True)
    )
    return {row.circle_id: row for row in rows.all()}


async def test_appraisals_and_assets_follow_circle(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.appraisals = [
        _appraisal("AP-1", "Completed", "2025-04-01"),
        _appraisal("AP-2", "HR Review Pending", "2026-04-01"),
        {"period": "no id, cannot be tracked"},
    ]
    fake.assets = [{"id": "AST-1", "name": "MacBook", "type": "Laptop", "status": "Assigned"}]
    assert set(await module.sync_records_for(seed.report.id)) >= {"appraisals", "assets"}
    appraisals = await _rows(db, EmployeeAppraisal, seed.report.id)
    assert set(appraisals) == {"AP-1", "AP-2"}
    assert appraisals["AP-1"].score == 4.0 and appraisals["AP-1"].revised_salary == "7 LPA"
    assert set(await _rows(db, EmployeeAsset, seed.report.id)) == {"AST-1"}

    # Circle updates one and drops the other.
    fake.appraisals = [_appraisal("AP-2", "Completed", "2026-04-01")]
    await module.sync_records_for(seed.report.id)
    appraisals = await _rows(db, EmployeeAppraisal, seed.report.id)
    assert set(appraisals) == {"AP-2"} and appraisals["AP-2"].status == "Completed"
    assert len(await _audits(db, "appraisals.circle_sync")) == 2


async def test_empty_lists_from_circle_never_wipe(
    scheduler: Scheduler, seed: _Seed, db: AsyncSession
) -> None:
    module, fake = scheduler
    fake.appraisals = [_appraisal("AP-1", "Completed", "2025-04-01")]
    fake.assets = [{"id": "AST-1", "name": "MacBook"}]
    await module.sync_records_for(seed.report.id)
    fake.appraisals, fake.assets = [], []
    assert await module.sync_records_for(seed.report.id) == []
    assert set(await _rows(db, EmployeeAppraisal, seed.report.id)) == {"AP-1"}
    assert set(await _rows(db, EmployeeAsset, seed.report.id)) == {"AST-1"}
