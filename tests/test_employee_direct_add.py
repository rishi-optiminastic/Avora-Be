"""Adding a member directly — no invite, no email, no account.

The point of the feature is that the resulting row is an ORDINARY employee: same
table, same fields, same visibility. These tests pin that (it shows up in the
directory, carries the profile it was given), plus the two things that make it
safe: only admin/HR may do it, and it cannot quietly resurrect or shadow someone
who is already on the roster.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

_URL = "/api/v1/employees"


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-direct",
        work_email="hr.direct@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


def _body(**over: object) -> dict[str, object]:
    return {"work_email": "contractor@acme.com", "full_name": "Cora Contractor", **over}


async def test_admin_adds_a_member_and_gets_an_ordinary_employee_back(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        _URL,
        json=_body(
            role="employee",
            department="Design",
            job_title="Illustrator",
            location="Ahmedabad",
            hire_date="2026-03-02",
        ),
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()

    assert created["work_email"] == "contractor@acme.com"
    assert created["full_name"] == "Cora Contractor"
    assert created["department"] == "Design"
    assert created["job_title"] == "Illustrator"
    assert created["hire_date"] == "2026-03-02"
    # Active from the moment they are added — an inactive row would 401 every
    # request and drop them out of attendance, leave and payroll.
    assert created["is_active"] is True
    assert created["status"] == EmployeeStatus.ACTIVE.value
    # Nobody accepted an invite, so the external id records the provenance.
    assert created["hr_external_id"].startswith("manual:")

    # And they are simply in the directory, like anyone else.
    listing = await client.get(_URL, headers=auth_headers(settings, seed.admin))
    assert listing.status_code == 200
    emails = {row["work_email"] for row in listing.json()["items"]}
    assert "contractor@acme.com" in emails


async def test_hr_may_add_but_a_manager_and_an_employee_may_not(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    hr = await _hr(db)

    for actor in (seed.manager, seed.report, seed.outsider):
        denied = await client.post(_URL, json=_body(), headers=auth_headers(settings, actor))
        assert denied.status_code == 403, f"{actor.role} should not add employees"

    allowed = await client.post(_URL, json=_body(), headers=auth_headers(settings, hr))
    assert allowed.status_code == 201, allowed.text


async def test_adding_someone_who_is_already_on_the_roster_is_a_conflict(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = auth_headers(settings, seed.admin)
    assert (await client.post(_URL, json=_body(), headers=headers)).status_code == 201

    # Case and padding must not sneak a duplicate past the check.
    clash = await client.post(
        _URL, json=_body(work_email="  Contractor@ACME.com  "), headers=headers
    )
    assert clash.status_code == 409, clash.text


async def test_a_deactivated_person_is_not_silently_recreated(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Offboarded rows still occupy the email. Reactivating is a separate,
    deliberate action — a create must not do it as a side effect."""
    headers = auth_headers(settings, seed.admin)
    created = await client.post(_URL, json=_body(), headers=headers)
    assert created.status_code == 201

    person = await db.get(Employee, uuid.UUID(created.json()["id"]))
    assert person is not None
    person.is_active = False
    person.status = EmployeeStatus.INACTIVE
    await db.commit()

    resp = await client.post(_URL, json=_body(), headers=headers)
    assert resp.status_code == 409
    await db.refresh(person)
    assert person.is_active is False


async def test_the_role_and_reporting_line_are_honoured(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        _URL,
        json=_body(role="manager", manager_id=str(seed.admin.id)),
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == Role.MANAGER.value
    assert resp.json()["manager_id"] == str(seed.admin.id)


async def test_an_unknown_manager_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        _URL,
        json=_body(manager_id=str(uuid.uuid4())),
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 422, resp.text


async def test_a_blank_name_is_rejected(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.post(
        _URL, json=_body(full_name="   "), headers=auth_headers(settings, seed.admin)
    )
    assert resp.status_code == 422, resp.text
