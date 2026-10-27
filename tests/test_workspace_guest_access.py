"""Granting and removing outside access from inside the app.

The companion to `test_workspace_guest.py`, which pins what a guest *is*. This
pins how one comes to exist without a shell on the server: an admin of this
workspace names an email, picks a role, and the API writes both halves - the
guest employee row that actually lets them in, and the membership row that
lists the workspace in their switcher.

Writing only one half is the bug this endpoint exists to stop. `POST /members`
grants membership alone, which looks like it worked and then 401s on arrival,
because every workspace keeps its own employees and nothing had created one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenancy import SHARED_SCHEMA
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.employee import EmployeeRepository
from app.repositories.workspace import WorkspaceRepository
from tests.conftest import _Seed, auth_headers

GUEST_EMAIL = "hq.hr@headquarters-group.com"


async def _register_this_workspace(db: AsyncSession, settings: Settings) -> Workspace:
    """`_require_admin_here` resolves the workspace from the live schema, so
    without a registry row every one of these endpoints is a 409."""
    workspace = Workspace(
        slug=settings.primary_workspace_slug,
        schema_name=SHARED_SCHEMA,
        name="Primary",
        location="",
        status=WorkspaceStatus.ACTIVE,
        provisioned_at=datetime.now(UTC),
    )
    db.add(workspace)
    await db.commit()
    return workspace


async def _add_person(db: AsyncSession, name: str, role: Role) -> Employee:
    person = Employee(
        hr_external_id=f"hr-{name.lower()}",
        work_email=f"{name.lower()}@workspace-one.com",
        full_name=name.title(),
        role=role,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(person)
    await db.commit()
    return person


def _invite(email: str = GUEST_EMAIL, role: str = "hr", name: str = "Hq Hr") -> dict[str, str]:
    return {"email": email, "role": role, "name": name}


# --------------------------------------------------------------------------- #
# Who may hand out access
# --------------------------------------------------------------------------- #
async def test_unauthenticated_is_rejected(client: AsyncClient, seed: _Seed) -> None:
    res = await client.post("/api/v1/workspaces/guests", json=_invite())
    assert res.status_code == 401


async def test_only_an_admin_may_grant_outside_access(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Letting an outside company read this one's payroll and attendance is a
    different act from administering its people, so HR is refused here even
    though HR can read the guest list."""
    await _register_this_workspace(db, settings)
    hr = await _add_person(db, "hana", Role.HR)
    for actor in (seed.report, seed.manager, seed.outsider, hr):
        res = await client.post(
            "/api/v1/workspaces/guests",
            json=_invite(),
            headers=auth_headers(settings, actor),
        )
        assert res.status_code == 403, f"{actor.work_email}: {res.text}"
    assert await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL) is None


async def test_only_an_admin_may_remove_outside_access(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _register_this_workspace(db, settings)
    hr = await _add_person(db, "hana", Role.HR)
    granted = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert granted.status_code == 201, granted.text
    for actor in (seed.report, seed.manager, hr):
        res = await client.delete(
            f"/api/v1/workspaces/guests/{GUEST_EMAIL}", headers=auth_headers(settings, actor)
        )
        assert res.status_code == 403, f"{actor.work_email}: {res.text}"
    still = await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL)
    assert still is not None and still.is_active


# --------------------------------------------------------------------------- #
# What a grant actually writes
# --------------------------------------------------------------------------- #
async def test_a_grant_writes_both_the_employee_row_and_the_membership(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The whole point. Membership alone lists the workspace in the switcher
    and then 401s on arrival; the employee row alone is invisible in it."""
    workspace = await _register_this_workspace(db, settings)
    res = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["is_guest"] is True
    assert body["role"] == "hr"
    assert body["is_active"] is True

    guest = await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL)
    assert guest is not None and guest.is_guest and guest.role is Role.HR
    assert await WorkspaceRepository(db).is_member(workspace.id, GUEST_EMAIL)


async def test_the_new_guest_is_not_this_workspace_s_personnel(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A grant must not enrol anybody in a payroll run. This is the failure
    that costs money, so it is asserted at the point of creation and not only
    on a hand-built row."""
    await _register_this_workspace(db, settings)
    res = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 201, res.text
    roster = await EmployeeRepository(db).list_all_active()
    assert GUEST_EMAIL not in {e.work_email for e in roster}
    assert seed.report.id in {e.id for e in roster}


async def test_the_guest_appears_on_the_outside_access_list(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _register_this_workspace(db, settings)
    await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    listed = await client.get(
        "/api/v1/employees/guests", headers=auth_headers(settings, seed.admin)
    )
    assert listed.status_code == 200, listed.text
    assert [row["work_email"] for row in listed.json()] == [GUEST_EMAIL]


async def test_the_name_falls_back_to_the_email(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Blank is allowed on purpose: the real name backfills from the identity
    provider on first sign-in, so insisting on one buys nothing."""
    await _register_this_workspace(db, settings)
    res = await client.post(
        "/api/v1/workspaces/guests",
        json={"email": GUEST_EMAIL, "role": "admin", "name": ""},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 201, res.text
    assert res.json()["full_name"] != ""


# --------------------------------------------------------------------------- #
# The role is chosen here, and only from the roles that mean anything
# --------------------------------------------------------------------------- #
async def test_a_guest_may_be_granted_hr_or_admin(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _register_this_workspace(db, settings)
    for role in ("hr", "admin"):
        res = await client.post(
            "/api/v1/workspaces/guests",
            json=_invite(email=f"{role}@headquarters-group.com", role=role),
            headers=auth_headers(settings, seed.admin),
        )
        assert res.status_code == 201, f"{role}: {res.text}"
        assert res.json()["role"] == role


async def test_the_org_chart_roles_are_refused(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Every refused role takes its scope from a position in THIS workspace -
    a reporting line, a department, an explicit grant. A guest holds none of
    them, so the account would be able to see nothing but its own empty record,
    which reads as a broken workspace rather than a deliberate restriction.
    """
    await _register_this_workspace(db, settings)
    for role in ("intern", "executive", "manager", "senior_manager", "viewer", "it_admin"):
        res = await client.post(
            "/api/v1/workspaces/guests",
            json=_invite(role=role),
            headers=auth_headers(settings, seed.admin),
        )
        assert res.status_code == 422, f"{role}: {res.status_code}"
    assert await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL) is None


async def test_a_bad_email_is_refused(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _register_this_workspace(db, settings)
    res = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(email="not-an-email"),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Never a back door into a real employee record
# --------------------------------------------------------------------------- #
async def test_an_existing_employee_cannot_be_turned_into_a_guest(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Flipping the flag on a real record would take their payslips,
    attendance and leave out of the figures those records belong to."""
    await _register_this_workspace(db, settings)
    staff = await _add_person(db, "priya", Role.EXECUTIVE)
    res = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(email=staff.work_email),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 409, res.text
    await db.refresh(staff)
    assert staff.is_guest is False
    assert staff.role is Role.EXECUTIVE


async def test_an_offboarded_employee_cannot_be_reopened_as_a_guest(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The inactive case is the dangerous one: that row is kept precisely
    because its history matters, and reviving it as a guest would quietly
    reclassify everything it produced while they worked here."""
    await _register_this_workspace(db, settings)
    leaver = await _add_person(db, "dev", Role.EXECUTIVE)
    leaver.is_active = False
    leaver.status = EmployeeStatus.INACTIVE
    await db.commit()
    res = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(email=leaver.work_email),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 409, res.text
    await db.refresh(leaver)
    assert leaver.is_guest is False
    assert leaver.is_active is False


async def test_a_differently_cased_email_finds_the_same_person(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Typed by hand, so the case will not match. An exact lookup would miss
    the row and the create would then hit the unique index as a 500."""
    await _register_this_workspace(db, settings)
    staff = await _add_person(db, "asha", Role.EXECUTIVE)
    res = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(email=staff.work_email.upper()),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 409, res.text


# --------------------------------------------------------------------------- #
# Re-granting
# --------------------------------------------------------------------------- #
async def test_re_granting_corrects_the_role_instead_of_failing(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Picking the wrong role is the likeliest mistake here, and the fix for it
    should be the same screen rather than a shell on the server."""
    await _register_this_workspace(db, settings)
    first = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(role="hr"),
        headers=auth_headers(settings, seed.admin),
    )
    assert first.status_code == 201, first.text
    again = await client.post(
        "/api/v1/workspaces/guests",
        json=_invite(role="admin"),
        headers=auth_headers(settings, seed.admin),
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["role"] == "admin"
    guests = await EmployeeRepository(db).list_guests()
    assert len(guests) == 1


async def test_re_granting_brings_a_removed_guest_back(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    workspace = await _register_this_workspace(db, settings)
    await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    removed = await client.delete(
        f"/api/v1/workspaces/guests/{GUEST_EMAIL}", headers=auth_headers(settings, seed.admin)
    )
    assert removed.status_code == 204, removed.text
    again = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert again.status_code == 201, again.text
    guest = await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL)
    assert guest is not None and guest.is_active and guest.is_guest
    assert await WorkspaceRepository(db).is_member(workspace.id, GUEST_EMAIL)


# --------------------------------------------------------------------------- #
# Removing access
# --------------------------------------------------------------------------- #
async def test_removing_a_guest_closes_both_halves(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A lingering membership keeps the workspace in their switcher, and
    picking it gives a baffling 401 rather than saying they no longer have
    access."""
    workspace = await _register_this_workspace(db, settings)
    await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    res = await client.delete(
        f"/api/v1/workspaces/guests/{GUEST_EMAIL}", headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 204, res.text
    guest = await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL)
    assert guest is not None, "soft-deleted, because the audit log points at this row"
    assert guest.is_active is False
    assert guest.status is EmployeeStatus.INACTIVE
    assert not await WorkspaceRepository(db).is_member(workspace.id, GUEST_EMAIL)
    assert await EmployeeRepository(db).list_guests() == []


async def test_removing_a_real_employee_through_this_route_is_refused(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Not an offboarding back door: that path is HR's, with its own rules."""
    await _register_this_workspace(db, settings)
    staff = await _add_person(db, "kiran", Role.EXECUTIVE)
    res = await client.delete(
        f"/api/v1/workspaces/guests/{staff.work_email}",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 404, res.text
    await db.refresh(staff)
    assert staff.is_active is True


async def test_removing_an_unknown_address_is_a_404(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _register_this_workspace(db, settings)
    res = await client.delete(
        "/api/v1/workspaces/guests/nobody@nowhere-at-all.com",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 404


async def test_an_admin_cannot_remove_their_own_access(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A guest admin administering a sister company is the normal case for this
    endpoint, and they must not be able to lock themselves out of it."""
    await _register_this_workspace(db, settings)
    seed.admin.is_guest = True
    await db.commit()
    res = await client.delete(
        f"/api/v1/workspaces/guests/{seed.admin.work_email}",
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 409, res.text
    await db.refresh(seed.admin)
    assert seed.admin.is_active is True


# --------------------------------------------------------------------------- #
# The audit trail
# --------------------------------------------------------------------------- #
async def test_both_acts_are_audited_with_the_role(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Who let an outside company into this one's data, and at what privilege,
    is exactly the question an audit log exists to answer later."""
    await _register_this_workspace(db, settings)
    await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    await client.delete(
        f"/api/v1/workspaces/guests/{GUEST_EMAIL}", headers=auth_headers(settings, seed.admin)
    )
    from sqlalchemy import select

    from app.models.audit import AuditLog

    rows = (await db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
    actions = [r.action for r in rows]
    assert "workspace.guest.add" in actions
    assert "workspace.guest.remove" in actions
    added = next(r for r in rows if r.action == "workspace.guest.add")
    assert added.target.endswith(f"{GUEST_EMAIL}:hr")
    assert added.actor == str(seed.admin.id)


# --------------------------------------------------------------------------- #
# A workspace nobody registered
# --------------------------------------------------------------------------- #
async def test_an_unregistered_workspace_says_so(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """No registry row means `bootstrap` was never run. A 409 naming the fix
    beats a 500, because the fix is a single command."""
    res = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 409, res.text


async def test_a_guest_hire_date_is_not_invented(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """They were never hired here, so there is no start date to record - and a
    today's-date placeholder would start a probation clock and accrue leave at
    a company that does not employ them."""
    await _register_this_workspace(db, settings)
    res = await client.post(
        "/api/v1/workspaces/guests", json=_invite(), headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 201, res.text
    guest = await EmployeeRepository(db).find_by_work_email_insensitive(GUEST_EMAIL)
    assert guest is not None
    assert guest.hire_date is None
    assert guest.hire_date != date.today()
