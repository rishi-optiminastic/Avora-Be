"""A guest holds an account in a workspace without being employed by it.

The case this exists for: an admin or an HR person at headquarters who has to
administer a sister company's workspace. Before `is_guest` there was one axis -
an employee row meant both "can sign in here" and "is personnel here" - so
granting that access also enrolled them in the other company's headcount, its
attendance report and, worst of all, its payroll run.

These tests pin the split from both sides, because either half failing is a
real incident: a guest who cannot act is a locked-out administrator, and a
guest who is counted is somebody drawing a salary from a company that never
hired them.
"""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from tests.conftest import _Seed, auth_headers


async def _guest(db: AsyncSession, *, role: Role = Role.ADMIN) -> Employee:
    """An HQ person with an account here and no employment here."""
    person = Employee(
        hr_external_id="manual:hq-guest",
        work_email="hq.admin@headquarters.test",
        full_name="Hq Guest",
        role=role,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        is_guest=True,
        hire_date=date(2020, 1, 1),
    )
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return person


# --------------------------------------------------------------------------- #
# Not personnel
# --------------------------------------------------------------------------- #
async def test_a_guest_is_not_on_the_payroll_roster(db: AsyncSession, seed: _Seed) -> None:
    """The one that would have cost real money.

    `list_all_active` is what a payroll run iterates to decide who gets a
    payslip. A guest appearing here is a salary paid by a company that never
    employed them.
    """
    guest = await _guest(db)
    roster = await EmployeeRepository(db).list_all_active()
    assert guest.id not in {e.id for e in roster}
    # The workspace's own people are untouched by the exclusion.
    assert seed.report.id in {e.id for e in roster}


async def test_a_guest_is_not_in_the_headcount(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The Team page counts what this endpoint returns, so a guest in it makes
    the company look one person bigger than it is."""
    guest = await _guest(db)
    before = await client.get("/api/v1/employees", headers=auth_headers(settings, seed.admin))
    assert before.status_code == 200, before.text
    body = before.json()
    emails = {row["work_email"] for row in body["items"]}
    assert guest.work_email not in emails
    assert body["total"] == len(emails)


async def test_a_guest_is_not_expected_at_work(db: AsyncSession, seed: _Seed) -> None:
    """`all_in_scope` feeds the attendance and activity rollups, and its whole
    point is to include people with no samples - which is exactly how a guest
    would be marked absent every single day."""
    guest = await _guest(db)
    caller = CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)
    everyone = await EmployeeRepository(db).all_in_scope(caller)
    assert guest.id not in {e.id for e in everyone}


async def test_a_guest_does_not_make_somebody_a_manager(db: AsyncSession, seed: _Seed) -> None:
    """`has_reports` decides whether someone is treated as a manager - it gates
    approvals and the whole reporting-tree scope. A guest pointed at a person
    by mistake must not promote them.

    Pointed at the outsider, who has nobody reporting to them, so the assertion
    is about the guest and not about the seed's existing org tree.
    """
    repo = EmployeeRepository(db)
    assert await repo.has_reports(seed.outsider.id) is False

    guest = await _guest(db)
    guest.manager_id = seed.outsider.id
    await db.commit()

    assert await repo.has_reports(seed.outsider.id) is False


# --------------------------------------------------------------------------- #
# Still able to act
# --------------------------------------------------------------------------- #
async def test_a_guest_admin_can_still_sign_in_and_use_their_role(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Being a guest changes employment, never privilege. An HQ admin is here
    to administer the place, so the roster has to open for them."""
    guest = await _guest(db)
    resp = await client.get("/api/v1/employees", headers=auth_headers(settings, guest))
    assert resp.status_code == 200, resp.text
    # They can see this workspace's people even though they are not among them.
    assert seed.report.work_email in {row["work_email"] for row in resp.json()["items"]}


async def test_a_guest_is_still_told_about_things_hr_must_action(db: AsyncSession) -> None:
    """Role lookups drive notifications - "tell HR a form is waiting". A guest
    HR is there precisely to answer those, so these queries deliberately do NOT
    carry the staff-only clause."""
    guest = await _guest(db, role=Role.HR)
    hr = await EmployeeRepository(db).list_by_role(Role.HR)
    assert guest.id in {e.id for e in hr}


async def test_a_guest_admin_is_still_an_admin_for_alerts(db: AsyncSession) -> None:
    guest = await _guest(db)
    admins = await EmployeeRepository(db).list_admins()
    assert guest.id in {e.id for e in admins}


# --------------------------------------------------------------------------- #
# Visible, on purpose
# --------------------------------------------------------------------------- #
async def test_this_workspace_can_see_who_has_outside_access(db: AsyncSession, seed: _Seed) -> None:
    """Every other roster hides guests. Without this list, nobody here could
    find out who outside the company can read their payroll and attendance -
    and that answer belongs to this workspace's own admins."""
    guest = await _guest(db)
    guests = await EmployeeRepository(db).list_guests()
    assert [e.id for e in guests] == [guest.id]
    # It is the complement of the staff list, not a second copy of it.
    assert seed.report.id not in {e.id for e in guests}


async def test_an_offboarded_guest_drops_off_the_outside_access_list(
    db: AsyncSession,
) -> None:
    """Revoking access has to be visible as revoked, or the list becomes a
    record of who used to be able to read this company's data."""
    guest = await _guest(db)
    guest.is_active = False
    guest.status = EmployeeStatus.INACTIVE
    await db.commit()
    assert await EmployeeRepository(db).list_guests() == []


# --------------------------------------------------------------------------- #
# Authorization on the outside-access list (CLAUDE §9)
# --------------------------------------------------------------------------- #
async def test_only_hr_and_admin_may_read_the_outside_access_list(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """It names people and the workspaces they act for. Useful to whoever owns
    this company's data, nobody else's business."""
    await _guest(db)
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get("/api/v1/employees/guests", headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.work_email}: {resp.status_code}"


async def test_an_admin_reads_the_outside_access_list(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    guest = await _guest(db)
    resp = await client.get("/api/v1/employees/guests", headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 200, resp.text
    assert [row["work_email"] for row in resp.json()] == [guest.work_email]
    assert resp.json()[0]["is_guest"] is True


async def test_the_guests_path_is_not_read_as_an_employee_id(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """`/employees/guests` and `/employees/{id}` share a prefix, and FastAPI
    matches in definition order. Declared the wrong way round this 422s on a
    uuid parse instead of listing anyone."""
    resp = await client.get("/api/v1/employees/guests", headers=auth_headers(settings, seed.admin))
    assert resp.status_code != 422
