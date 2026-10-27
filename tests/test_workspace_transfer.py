"""Moving one person from one workspace to another.

An offboard in the workspace they leave and a joiner in the one they arrive
at. Their payslips, attendance and leave stay behind, because those belong to
the legal entity that paid and employed them - a payslip cannot be reassigned
to a different company. Their identity travels; their ROLE never does.

**On the test database the two workspaces share one schema.** SQLite has none,
so `use_schema` is a no-op and both "workspaces" are the same `employees`
table. That makes the authorization and validation rules fully testable, and
it makes the happy path collide on `uq_employees_work_email` - which is used
here deliberately to prove the atomicity: a target-side failure must roll the
offboard back, never leave somebody deactivated in one place and absent from
the other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenancy import SHARED_SCHEMA
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.workspace import WorkspaceRepository
from tests.conftest import _Seed, auth_headers


async def _workspace(
    db: AsyncSession, slug: str, *, provisioned: bool = True, schema: str | None = None
) -> Workspace:
    workspace = Workspace(
        slug=slug,
        schema_name=schema or SHARED_SCHEMA,
        name=slug.title(),
        location="",
        status=WorkspaceStatus.ACTIVE,
        provisioned_at=datetime.now(UTC) if provisioned else None,
    )
    db.add(workspace)
    await db.commit()
    return workspace


async def _setup(db: AsyncSession, settings: Settings, admin: Employee) -> Workspace:
    """This workspace plus a second one the admin may open."""
    await _workspace(db, settings.primary_workspace_slug)
    target = await _workspace(db, "acme", schema="ws_acme")
    repo = WorkspaceRepository(db)
    await repo.grant(target.id, admin.work_email, granted_by_email=None)
    await db.commit()
    return target


async def _transfer(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    employee: Employee,
    slug: str = "acme",
):
    return await client.post(
        "/api/v1/workspaces/transfer",
        json={"employee_id": str(employee.id), "target_slug": slug},
        headers=auth_headers(settings, who),
    )


# --------------------------------------------------------------------------- #
# Who may move somebody
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    res = await client.post(
        "/api/v1/workspaces/transfer",
        json={"employee_id": str(seed.report.id), "target_slug": "acme"},
    )
    assert res.status_code == 401


async def test_an_ordinary_employee_cannot_transfer(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(db, settings, seed.admin)
    for actor in (seed.report, seed.manager, seed.outsider):
        res = await _transfer(client, settings, actor, seed.report)
        assert res.status_code == 403, f"{actor.full_name}: {res.text}"


async def _hr(db: AsyncSession) -> Employee:
    person = Employee(
        hr_external_id="hr-hana",
        work_email="hana@corp.io",
        full_name="Hana Hr",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


async def test_hr_can_transfer(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A transfer is an offboard here and a joiner there, and HR does both
    every week. Gating it on admin left the people who actually run
    inter-entity moves looking at a profile with no button on it.

    The 409 is the SQLite collision the module docstring describes - both
    workspaces share one `employees` table, so the arrival hits the unique work
    email. What it proves here is that HR got through the authorization gate:
    a refusal would be 403 and would never reach the target at all.
    """
    hr = await _hr(db)
    target = await _setup(db, settings, seed.admin)
    await WorkspaceRepository(db).grant(target.id, hr.work_email, granted_by_email=None)
    await db.commit()

    res = await _transfer(client, settings, hr, seed.report)

    assert res.status_code != 403, res.text
    assert res.status_code == 409, res.text


async def test_hr_still_cannot_send_someone_where_hr_cannot_go(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Widening the caller set must not widen the destinations. Without this,
    HR could inject people into a company whose workspace it was never let
    into."""
    hr = await _hr(db)
    await _setup(db, settings, seed.admin)  # granted to the admin, not to HR

    res = await _transfer(client, settings, hr, seed.report)

    assert res.status_code == 403, res.text


async def test_hr_still_cannot_hand_out_access(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Moving a person is people work; letting another company read this one's
    payroll is not. The wider gate is on `transfer` alone."""
    hr = await _hr(db)
    await _setup(db, settings, seed.admin)

    for call in (
        client.get("/api/v1/workspaces/members", headers=auth_headers(settings, hr)),
        client.post(
            "/api/v1/workspaces/members",
            json={"email": "someone@corp.io"},
            headers=auth_headers(settings, hr),
        ),
        client.post(
            "/api/v1/workspaces/guests",
            json={"email": "someone@corp.io", "name": "", "role": "hr"},
            headers=auth_headers(settings, hr),
        ),
        client.delete(
            f"/api/v1/workspaces/guests/{hr.work_email}", headers=auth_headers(settings, hr)
        ),
    ):
        res = await call
        assert res.status_code == 403, res.text


async def test_you_cannot_send_someone_to_a_workspace_you_cannot_open(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Otherwise an admin of one company could inject people into another
    company's roster from outside it - the exact thing separate workspaces
    exist to prevent."""
    await _workspace(db, settings.primary_workspace_slug)
    await _workspace(db, "acme", schema="ws_acme")  # exists; never granted

    res = await _transfer(client, settings, seed.admin, seed.report)

    assert res.status_code == 403, res.text


# --------------------------------------------------------------------------- #
# What may be asked
# --------------------------------------------------------------------------- #
async def test_an_unknown_workspace_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(db, settings, seed.admin)
    res = await _transfer(client, settings, seed.admin, seed.report, slug="nowhere")
    assert res.status_code == 422, res.text


async def test_a_workspace_that_never_finished_building_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _workspace(db, settings.primary_workspace_slug)
    half = await _workspace(db, "halfbuilt", provisioned=False, schema="ws_halfbuilt")
    repo = WorkspaceRepository(db)
    await repo.grant(half.id, seed.admin.work_email, granted_by_email=None)
    await db.commit()

    res = await _transfer(client, settings, seed.admin, seed.report, slug="halfbuilt")

    assert res.status_code == 422, res.text


async def test_transferring_into_the_same_workspace_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(db, settings, seed.admin)
    res = await _transfer(
        client, settings, seed.admin, seed.report, slug=settings.primary_workspace_slug
    )
    assert res.status_code == 422, res.text


async def test_an_unknown_person_is_a_404(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(db, settings, seed.admin)
    res = await client.post(
        "/api/v1/workspaces/transfer",
        json={"employee_id": str(uuid.uuid4()), "target_slug": "acme"},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 404, res.text


async def test_a_malformed_target_slug_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(db, settings, seed.admin)
    res = await _transfer(client, settings, seed.admin, seed.report, slug="DROP; SCHEMA")
    assert res.status_code == 422, res.text


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #
async def test_a_failed_arrival_rolls_the_departure_back(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The property that matters most.

    Here the arrival fails because both workspaces share one `employees` table
    on SQLite and the work email is unique in it. Whatever the cause, the
    person must not be left deactivated in the workspace they were leaving and
    absent from the one they were joining.
    """
    await _setup(db, settings, seed.admin)

    # Captured before expiring: reading it off the ORM object afterwards would
    # need a lazy refresh, which cannot run from a sync attribute access.
    employee_id = seed.report.id

    res = await _transfer(client, settings, seed.admin, seed.report)

    assert res.status_code == 409, res.text
    db.expire_all()
    still_here = await db.get(Employee, employee_id)
    assert still_here is not None
    assert still_here.is_active is True, "the offboard must roll back with the failed arrival"


async def test_leaving_a_workspace_takes_the_grant_to_open_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A stale grant leaves the old workspace in their switcher, where picking
    it gives a baffling 401 instead of "you no longer work there"."""
    target = await _setup(db, settings, seed.admin)
    source = await WorkspaceRepository(db).get_by_slug(settings.primary_workspace_slug)
    assert source is not None
    repo = WorkspaceRepository(db)
    await repo.grant(source.id, seed.report.work_email, granted_by_email=None)
    await db.commit()
    # Captured before expiring: reading them off the ORM objects afterwards
    # would need a lazy refresh, which cannot run from a sync attribute access.
    source_id, email = source.id, seed.report.work_email
    assert await repo.is_member(source_id, email)

    await _transfer(client, settings, seed.admin, seed.report)

    db.expire_all()
    # The transfer itself fails on SQLite's shared table, so this asserts the
    # revoke rolls back WITH it - the grant must not be lost by a transfer that
    # did not happen.
    assert await repo.is_member(source_id, email)
    assert target is not None


async def test_a_guest_cannot_be_transferred(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A guest holds access to this workspace on behalf of another one, so
    there is no employment here to move.

    Transferring one would offboard an outside administrator, revoke the grant
    that lets them open this workspace, and recreate them in the target as an
    ordinary executive - silently demoting a cross-workspace admin into a
    member of staff, and taking their access away in the process. Every step of
    that succeeds individually, which is why it has to be refused up front.
    """
    await _setup(db, settings, seed.admin)
    guest = await db.get(Employee, seed.report.id)
    assert guest is not None
    guest.is_guest = True
    await db.commit()

    res = await client.post(
        "/api/v1/workspaces/transfer",
        json={"employee_id": str(seed.report.id), "target_slug": "acme"},
        headers=auth_headers(settings, seed.admin),
    )

    assert res.status_code == 409, res.text
    assert "nothing to transfer" in res.json()["error"]["message"]

    # Refused means untouched: still active, still a guest, still here.
    await db.refresh(guest)
    assert guest.is_active is True
    assert guest.is_guest is True
