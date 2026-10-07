"""HR webhook: unsigned rejection + cannot set privilege (Security rule 5.5)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import AuditLog, Employee, EmployeeStatus, Role
from tests.conftest import _Seed, hr_headers


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "hr_external_id": "hr-new-1",
        "work_email": "newhire@corp.io",
        "full_name": "Nina Newhire",
        "department": "Engineering",
        "manager_external_id": None,
        "status": "active",
        "start_date": None,
    }
    body.update(overrides)
    return body


async def test_unsigned_webhook_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    resp = await client.post("/api/v1/hr/sync", json=_payload())
    assert resp.status_code == 401


async def test_bad_signature_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(settings, _payload())
    headers["X-HR-Signature"] = "sha256=00000000"
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_valid_webhook_creates_employee(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(settings, _payload())
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["hr_external_id"] == "hr-new-1"
    # HR-created employees default to the least-privileged role.
    assert body["role"] == "employee"


async def test_webhook_cannot_set_role(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # Even if HR smuggles a `role` field, the strict schema ignores it and the
    # employee is created as a plain employee — privilege never escalates.
    raw, headers = hr_headers(settings, _payload(role="admin"))
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "employee"

    created = await db.scalar(select(Employee).where(Employee.hr_external_id == "hr-new-1"))
    assert created is not None
    assert created.role is Role.EMPLOYEE


async def test_webhook_offboard_soft_deletes(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    raw, headers = hr_headers(
        settings,
        _payload(hr_external_id="hr-report", work_email="report@corp.io", status="inactive"),
    )
    resp = await client.post("/api/v1/hr/sync", content=raw, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["status"] == "inactive"


async def _post(client: AsyncClient, settings: Settings, body: dict[str, object]) -> Response:
    raw, headers = hr_headers(settings, body)
    return await client.post("/api/v1/hr/sync", content=raw, headers=headers)


async def _add_employee(
    db: AsyncSession,
    *,
    hr_external_id: str,
    work_email: str,
    role: Role = Role.EMPLOYEE,
    manager: Employee | None = None,
) -> Employee:
    # The seed's `.test` addresses fail EmailStr, so payload-facing rows use corp.io.
    employee = Employee(
        hr_external_id=hr_external_id,
        work_email=work_email,
        full_name=work_email.split("@")[0].title(),
        role=role,
        manager_id=manager.id if manager else None,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(employee)
    await db.commit()
    return employee


async def test_sync_claims_invited_placeholder_by_email(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # Someone invited before HR knew them: the first sync must adopt that row
    # (keeping their role) instead of creating a duplicate person.
    invited = await _add_employee(
        db, hr_external_id="invite:abc", work_email="pat@corp.io", role=Role.MANAGER
    )

    resp = await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="PAT@corp.io", full_name="Pat Real"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(invited.id)
    assert body["hr_external_id"] == "EMP-1001"
    assert body["role"] == "manager"
    # Stored lower-case: login matches the token email exactly, so keeping
    # HR's capitals would lock this person out.
    assert body["work_email"] == "pat@corp.io"

    rows = (await db.scalars(select(Employee).where(Employee.full_name == "Pat Real"))).all()
    assert len(rows) == 1


async def test_sync_refuses_email_owned_by_another_hr_record(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="EMP-1001", work_email="taken@corp.io")
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-2002", work_email="taken@corp.io")
    )
    assert resp.status_code == 409


async def test_sync_refuses_email_change_onto_another_employee(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="EMP-1001", work_email="one@corp.io")
    await _add_employee(db, hr_external_id="EMP-1002", work_email="two@corp.io")
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-1001", work_email="two@corp.io")
    )
    assert resp.status_code == 409


async def test_omitted_manager_keeps_reporting_edge(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(
        db, hr_external_id="EMP-1001", work_email="one@corp.io", manager=seed.manager
    )
    body = _payload(hr_external_id="EMP-1001", work_email="one@corp.io")
    del body["manager_external_id"]
    resp = await _post(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["manager_id"] == str(seed.manager.id)


async def test_explicit_null_manager_clears_reporting_edge(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(
        db, hr_external_id="EMP-1001", work_email="one@corp.io", manager=seed.manager
    )
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-1001", work_email="one@corp.io")
    )
    assert resp.status_code == 200
    assert resp.json()["manager_id"] is None


async def test_job_title_only_overwritten_when_sent(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await _post(client, settings, _payload(job_title="Print Operator"))
    assert resp.json()["job_title"] == "Print Operator"

    resp = await _post(client, settings, _payload())
    assert resp.json()["job_title"] == "Print Operator"


async def test_an_unresolvable_manager_keeps_the_reporting_edge(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A manager HR knows about but the PMS has not created yet is NOT a request
    to clear the edge.

    In a bulk sync a report routinely arrives before their manager. Treating the
    unresolved id as null silently demoted the lead: the reporting edge is what
    grants access to their team, so their reports' leave and regularizations
    became invisible and un-actionable with nothing logged.
    """
    await _add_employee(
        db, hr_external_id="EMP-1001", work_email="one@corp.io", manager=seed.manager
    )
    body = _payload(hr_external_id="EMP-1001", work_email="one@corp.io")
    body["manager_external_id"] = "EMP-NOT-SYNCED-YET"
    resp = await _post(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["manager_id"] == str(seed.manager.id)


async def test_capitalised_email_is_stored_lowercase(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await _post(client, settings, _payload(work_email="New.Hire@Corp.io"))
    assert resp.status_code == 200 and resp.json()["work_email"] == "new.hire@corp.io"


@pytest.mark.parametrize("external_id", ["manual:x", "invite:y"])
async def test_reserved_external_id_prefixes_are_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed, external_id: str
) -> None:
    resp = await _post(client, settings, _payload(hr_external_id=external_id))
    assert resp.status_code == 422


async def test_empty_manager_value_keeps_reporting_edge(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(
        db, hr_external_id="EMP-1001", work_email="one@corp.io", manager=seed.manager
    )
    resp = await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="one@corp.io", manager_external_id=""),
    )
    assert resp.status_code == 200
    assert resp.json()["manager_id"] == str(seed.manager.id)


async def test_hr_cannot_move_a_privileged_accounts_email(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    # Login is keyed on email: re-pointing an admin's email would hand admin to
    # whoever owns the new address.
    await _add_employee(db, hr_external_id="EMP-1001", work_email="boss@corp.io", role=Role.ADMIN)
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-1001", work_email="someone@corp.io")
    )
    assert resp.status_code == 409


async def test_hr_can_change_an_ordinary_employees_email(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="EMP-1001", work_email="old@corp.io")
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-1001", work_email="new@corp.io")
    )
    assert resp.status_code == 200 and resp.json()["work_email"] == "new@corp.io"
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "hr.email_change" in actions


async def test_case_variant_duplicates_are_a_conflict_not_a_guess(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="invite:a", work_email="Dup@corp.io")
    await _add_employee(db, hr_external_id="invite:b", work_email="dup@corp.io")
    resp = await _post(
        client, settings, _payload(hr_external_id="EMP-3001", work_email="dup@corp.io")
    )
    assert resp.status_code == 409


async def test_location_and_employee_number_are_synced(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    resp = await _post(client, settings, _payload(location="Mumbai", employee_number="EMP-5983"))
    assert resp.json()["location"] == "Mumbai"
    created = await db.scalar(select(Employee).where(Employee.hr_external_id == "hr-new-1"))
    assert created is not None and created.employee_number == "EMP-5983"


async def test_employee_number_never_replaces_one_payroll_set(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    emp = await _add_employee(db, hr_external_id="EMP-1001", work_email="one@corp.io")
    emp.employee_number = "PAY-0042"
    await db.commit()
    await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="one@corp.io", employee_number="EMP-1001"),
    )
    await db.refresh(emp)
    assert emp.employee_number == "PAY-0042"


async def test_employee_number_fills_an_empty_one(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    emp = await _add_employee(db, hr_external_id="EMP-1001", work_email="one@corp.io")
    await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="one@corp.io", employee_number="EMP-1001"),
    )
    await db.refresh(emp)
    assert emp.employee_number == "EMP-1001"


async def test_hire_date_is_set_from_the_joining_date(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await _post(client, settings, _payload(start_date="2026-08-21T00:00:00"))
    assert resp.json()["hire_date"] == "2026-08-21"


async def test_hr_cannot_deactivate_a_privileged_account(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="EMP-1001", work_email="boss@corp.io", role=Role.ADMIN)
    resp = await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="boss@corp.io", status="inactive"),
    )
    assert resp.status_code == 409


async def test_hr_can_deactivate_an_ordinary_employee(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _add_employee(db, hr_external_id="EMP-1001", work_email="one@corp.io")
    resp = await _post(
        client,
        settings,
        _payload(hr_external_id="EMP-1001", work_email="one@corp.io", status="inactive"),
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False
