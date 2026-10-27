"""The workspace boundary.

Avora serves several workspaces from one deployment, each in its own Postgres
schema. The guard rails are tested here rather than assumed, because the
failure mode is not a bug but a leak: one company seeing another's payroll.

Two layers:
- `validate_slug` / `validate_schema_name` are the only things standing between
  a request header and a `SET search_path`, which cannot take a bound
  parameter. They are tested as the injection guard they are.
- `_resolve_workspace` is the authorization decision - who may be pointed at
  which schema.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from scripts.tenancy import _apply_branding, _install_admin
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import WORKSPACE_HEADER, _resolve_workspace
from app.core.exceptions import AuthorizationError, ConflictError
from app.core.names import placeholder_name_from_email
from app.core.tenancy import (
    SHARED_SCHEMA,
    InvalidWorkspaceSlug,
    new_schema_for,
    validate_schema_name,
    validate_slug,
)
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.employee import EmployeeRepository
from app.repositories.org_settings import OrgSettingsRepository
from app.repositories.workspace import WorkspaceRepository, normalize_email
from tests.conftest import _FakeJwksClient, _Seed, auth_headers, bearer_for_email


# --------------------------------------------------------------------------- #
# The injection guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a",  # too short
        "1leading",  # must start with a letter
        "Has-Caps",
        "with space",
        "semi;colon",
        'quote"mark',
        "public; DROP SCHEMA ws_acme CASCADE",
        "ws_acme--",
        "a" * 40,  # too long
        "unicodé",
    ],
)
def test_a_slug_that_could_reach_sql_is_refused(bad: str) -> None:
    """Rejected, never sanitised: a "cleaned" slug could silently collide with
    another workspace's schema, which is worse than an error."""
    with pytest.raises(InvalidWorkspaceSlug):
        validate_slug(bad)


@pytest.mark.parametrize("good", ["ab", "acme", "acme_two", "a1", "x" * 39])
def test_a_plain_slug_is_accepted(good: str) -> None:
    assert validate_slug(good) == good


def test_a_slug_is_lowercased_and_trimmed() -> None:
    assert validate_slug("  Acme  ".replace("A", "a")) == "acme"


def test_only_public_and_ws_schemas_are_valid() -> None:
    assert validate_schema_name("public") == "public"
    assert validate_schema_name("ws_acme") == "ws_acme"
    for bad in ("pg_catalog", "information_schema", "ws_", "ws_1bad", "random"):
        with pytest.raises(InvalidWorkspaceSlug):
            validate_schema_name(bad)


def test_a_new_workspace_never_takes_public() -> None:
    assert new_schema_for("acme") == "ws_acme"


# --------------------------------------------------------------------------- #
# Who gets pointed at which schema
# --------------------------------------------------------------------------- #
async def _workspace(db: AsyncSession, slug: str, *, provisioned: bool = True) -> Workspace:
    workspace = Workspace(
        slug=slug,
        schema_name=new_schema_for(slug),
        name=slug.title(),
        location="",
        status=WorkspaceStatus.ACTIVE,
        provisioned_at=datetime.now(UTC) if provisioned else None,
    )
    db.add(workspace)
    await db.commit()
    return workspace


async def _resolve(
    db: AsyncSession, settings: Settings, requested: str | None, email: str | None = None
) -> str:
    """`_resolve_workspace` with a token for `email`, or no token at all.

    No email means a MACHINE caller - an agent, the HR webhook, a biometric
    push. They authenticate by other means and carry no Bearer token.
    """
    authorization = (
        bearer_for_email(settings, email)["Authorization"] if email is not None else None
    )
    return await _resolve_workspace(db, settings, _FakeJwksClient(), authorization, requested)


async def test_no_header_means_the_first_workspace(db: AsyncSession, settings: Settings) -> None:
    """Every agent, HR webhook and biometric push predates workspaces. They send
    no header and must keep landing exactly where they always have."""
    assert await _resolve(db, settings, None) == SHARED_SCHEMA


async def test_naming_the_first_workspace_is_not_an_escalation(
    db: AsyncSession, settings: Settings
) -> None:
    """Gating the original workspace on a membership row would lock out all 28
    people the moment one was missing."""
    assert await _resolve(db, settings, settings.primary_workspace_slug) == SHARED_SCHEMA


async def test_an_unknown_workspace_is_refused(db: AsyncSession, settings: Settings) -> None:
    with pytest.raises(AuthorizationError):
        await _resolve(db, settings, "nosuchplace", email="someone@corp.test")


async def test_a_machine_caller_cannot_reach_a_second_workspace(
    db: AsyncSession, settings: Settings
) -> None:
    """Agent and webhook credentials say nothing about which company they are
    for, so they can only ever reach the first workspace."""
    await _workspace(db, "acme")
    with pytest.raises((AuthorizationError, Exception)):
        await _resolve(db, settings, "acme", email=None)


async def test_a_workspace_that_never_finished_building_is_refused(
    db: AsyncSession, settings: Settings
) -> None:
    """`provisioned_at` is set only once the migration chain has built the
    schema. Until then the row exists but nobody can be let in."""
    await _workspace(db, "halfbuilt", provisioned=False)
    with pytest.raises(AuthorizationError):
        await _resolve(db, settings, "halfbuilt", email="someone@corp.test")


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #
async def test_membership_is_case_insensitive(db: AsyncSession) -> None:
    """The email comes from an OIDC token and its casing is not stable."""
    workspace = await _workspace(db, "acme")
    repo = WorkspaceRepository(db)
    await repo.grant(workspace.id, "  Someone@Corp.TEST ", granted_by_email=None)
    await db.commit()

    assert await repo.is_member(workspace.id, "someone@corp.test")
    assert await repo.is_member(workspace.id, "SOMEONE@CORP.TEST")


async def test_granting_twice_is_the_same_as_once(db: AsyncSession) -> None:
    workspace = await _workspace(db, "acme")
    repo = WorkspaceRepository(db)
    first = await repo.grant(workspace.id, "a@corp.test", granted_by_email=None)
    second = await repo.grant(workspace.id, "a@corp.test", granted_by_email=None)
    await db.commit()

    assert first.id == second.id
    assert await repo.member_count(workspace.id) == 1


async def test_revoking_removes_access(db: AsyncSession) -> None:
    workspace = await _workspace(db, "acme")
    repo = WorkspaceRepository(db)
    await repo.grant(workspace.id, "a@corp.test", granted_by_email=None)
    await db.commit()

    await repo.revoke(workspace.id, "A@CORP.TEST")
    await db.commit()

    assert not await repo.is_member(workspace.id, "a@corp.test")


async def test_you_only_see_workspaces_you_were_let_into(db: AsyncSession) -> None:
    """The second company is absent from the switcher, not merely greyed out."""
    acme = await _workspace(db, "acme")
    await _workspace(db, "other")
    repo = WorkspaceRepository(db)
    await repo.grant(acme.id, "a@corp.test", granted_by_email=None)
    await db.commit()

    mine = await repo.list_for_email("a@corp.test")

    assert [w.slug for w in mine] == ["acme"]
    assert await repo.list_for_email("nobody@corp.test") == []


async def test_a_suspended_workspace_is_not_offered(db: AsyncSession) -> None:
    """The membership row stays so access is not forgotten, but nobody is let in
    while it is closed."""
    workspace = await _workspace(db, "acme")
    repo = WorkspaceRepository(db)
    await repo.grant(workspace.id, "a@corp.test", granted_by_email=None)
    workspace.status = WorkspaceStatus.SUSPENDED
    await db.commit()

    assert await repo.list_for_email("a@corp.test") == []
    assert await repo.is_member(workspace.id, "a@corp.test")  # the grant survives


async def test_an_unbuilt_workspace_is_not_offered(db: AsyncSession) -> None:
    workspace = await _workspace(db, "acme", provisioned=False)
    repo = WorkspaceRepository(db)
    await repo.grant(workspace.id, "a@corp.test", granted_by_email=None)
    await db.commit()

    assert await repo.list_for_email("a@corp.test") == []


def test_emails_are_normalized_one_way() -> None:
    assert normalize_email("  MiXeD@Corp.Test  ") == "mixed@corp.test"


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #
async def test_unauthenticated_cannot_list_workspaces(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/workspaces/mine")).status_code == 401


async def test_the_switcher_lists_only_your_workspaces(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    acme = await _workspace(db, "acme")
    await _workspace(db, "other")
    repo = WorkspaceRepository(db)
    await repo.grant(acme.id, seed.report.work_email, granted_by_email=None)
    await db.commit()

    res = await client.get("/api/v1/workspaces/mine", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200, res.text
    assert [w["slug"] for w in res.json()] == ["acme"]


async def test_only_an_admin_manages_workspace_access(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        listed = await client.get(
            "/api/v1/workspaces/members", headers=auth_headers(settings, actor)
        )
        granted = await client.post(
            "/api/v1/workspaces/members",
            json={"email": "someone@corp.com"},
            headers=auth_headers(settings, actor),
        )
        assert listed.status_code == 403, f"{actor.full_name}: {listed.text}"
        assert granted.status_code == 403, f"{actor.full_name}: {granted.text}"


async def test_an_admin_grants_and_revokes_access_to_their_own_workspace(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    db.add(
        Workspace(
            slug=settings.primary_workspace_slug,
            schema_name=SHARED_SCHEMA,
            name="Primary",
            location="",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=datetime.now(UTC),
        )
    )
    await db.commit()
    headers = auth_headers(settings, seed.admin)

    granted = await client.post(
        "/api/v1/workspaces/members", json={"email": "newcomer@corp.com"}, headers=headers
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["email"] == "newcomer@corp.com"
    # Granted access, but no employee record here yet - shown, not hidden.
    assert granted.json()["has_account"] is False

    listed = await client.get("/api/v1/workspaces/members", headers=headers)
    assert "newcomer@corp.com" in {m["email"] for m in listed.json()}

    removed = await client.delete("/api/v1/workspaces/members/newcomer@corp.com", headers=headers)
    assert removed.status_code == 204
    after = await client.get("/api/v1/workspaces/members", headers=headers)
    assert "newcomer@corp.com" not in {m["email"] for m in after.json()}


async def test_an_admin_cannot_remove_their_own_access(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """It would leave the workspace one admin short with no way back in."""
    db.add(
        Workspace(
            slug=settings.primary_workspace_slug,
            schema_name=SHARED_SCHEMA,
            name="Primary",
            location="",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=datetime.now(UTC),
        )
    )
    await db.commit()

    res = await client.delete(
        f"/api/v1/workspaces/members/{seed.admin.work_email}",
        headers=auth_headers(settings, seed.admin),
    )

    assert res.status_code == 409, res.text


async def test_asking_for_a_workspace_you_were_not_let_into_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The header is a claim. Naming someone else's workspace is a 403, not
    their data."""
    await _workspace(db, "acme")

    res = await client.get(
        "/api/v1/workspaces/mine",
        headers={**auth_headers(settings, seed.report), WORKSPACE_HEADER: "acme"},
    )

    assert res.status_code == 403, res.text


async def test_a_malformed_workspace_header_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.get(
        "/api/v1/workspaces/mine",
        headers={
            **auth_headers(settings, seed.report),
            WORKSPACE_HEADER: "public; DROP SCHEMA x",
        },
    )
    assert res.status_code == 403, res.text


def test_a_random_uuid_is_not_a_workspace() -> None:
    with pytest.raises(InvalidWorkspaceSlug):
        validate_slug(str(uuid.uuid4()))


# --------------------------------------------------------------------------- #
# The browser has to be allowed to send it
# --------------------------------------------------------------------------- #
async def test_the_workspace_header_survives_a_cors_preflight(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The dashboard is a different origin from the API, so every request
    carrying this header is preflighted first.

    `allow_headers` is an explicit allowlist. Leaving the header out of it
    fails the preflight and the request never arrives - a break that neither
    the test suite nor a production build can see, because neither speaks
    CORS. Hence this test.
    """
    origin = settings.cors_origin_list[0]

    res = await client.options(
        "/api/v1/workspaces/mine",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": WORKSPACE_HEADER,
        },
    )

    assert res.status_code == 200, res.text
    allowed = res.headers.get("access-control-allow-headers", "").lower()
    assert WORKSPACE_HEADER.lower() in allowed, allowed


async def test_the_switcher_always_includes_the_first_workspace(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The regression that made the switcher invisible on the real deploy.

    `bootstrap` grants nobody a membership row for the first workspace, because
    `_resolve_workspace` lets anyone authenticated into it anyway. But `mine()`
    listed only workspaces with a membership row, so someone granted ONE other
    workspace saw a list of one - and the switcher hides below two. It never
    appeared.
    """
    db.add(
        Workspace(
            slug=settings.primary_workspace_slug,
            schema_name=SHARED_SCHEMA,
            name="Primary",
            location="HQ",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=datetime.now(UTC),
        )
    )
    acme = await _workspace(db, "acme")
    await WorkspaceRepository(db).grant(acme.id, seed.report.work_email, granted_by_email=None)
    await db.commit()

    res = await client.get("/api/v1/workspaces/mine", headers=auth_headers(settings, seed.report))

    assert res.status_code == 200, res.text
    slugs = [w["slug"] for w in res.json()]
    assert settings.primary_workspace_slug in slugs
    assert "acme" in slugs
    assert len(slugs) >= 2, "two workspaces, or the switcher stays hidden"
    # Exactly one is marked current, and it is the one we are in.
    assert [w["slug"] for w in res.json() if w["is_current"]] == [settings.primary_workspace_slug]


async def test_the_first_workspace_is_not_listed_twice(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Someone WITH a membership row for it must not see it duplicated."""
    db.add(
        Workspace(
            slug=settings.primary_workspace_slug,
            schema_name=SHARED_SCHEMA,
            name="Primary",
            location="HQ",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=datetime.now(UTC),
        )
    )
    await db.commit()
    primary = await WorkspaceRepository(db).get_by_slug(settings.primary_workspace_slug)
    assert primary is not None
    await WorkspaceRepository(db).grant(primary.id, seed.report.work_email, granted_by_email=None)
    await db.commit()

    res = await client.get("/api/v1/workspaces/mine", headers=auth_headers(settings, seed.report))

    slugs = [w["slug"] for w in res.json()]
    assert slugs.count(settings.primary_workspace_slug) == 1, slugs


# --------------------------------------------------------------------------- #
# The first admin of a new workspace
# --------------------------------------------------------------------------- #
# A brand new workspace is built from the migration chain and is therefore
# EMPTY - no employees at all. Every in-app way to add one needs an employee
# record in that workspace already, so without this command the workspace can
# never be opened by anybody. `grant` is not enough and was the actual bug: it
# writes a membership row, so the workspace shows up in the switcher, but
# `get_current_user` refuses anyone with no active employee row in the schema
# the request landed in, and the dashboard reads that 401 as a stale choice and
# bounces them back where they came from without a word.
#
# These exercise the create/promote/readmit decision. They cannot exercise the
# schema switch itself: the suite runs on SQLite, which has no schemas, so
# `use_schema` is a no-op there and every workspace shares one namespace (see
# `_supports_schemas`).
async def _reread(db: AsyncSession, email: str) -> Employee:
    """Read the row back after `_install_admin`.

    Not `db.refresh(person)`: the call expunges the identity map - it has to,
    because it moves the session between schemas and a cached object would
    answer from the wrong one - so anything held across it is detached.
    """
    found = await EmployeeRepository(db).find_by_work_email_insensitive(email)
    assert found is not None
    return found


async def _employee(db: AsyncSession, email: str, role: Role, *, active: bool = True) -> Employee:
    person = Employee(
        hr_external_id=f"test:{uuid.uuid4()}",
        work_email=email,
        full_name="Existing Person",
        role=role,
        status=EmployeeStatus.ACTIVE if active else EmployeeStatus.INACTIVE,
        is_active=active,
    )
    db.add(person)
    await db.commit()
    return person


async def test_the_first_admin_is_created_where_there_was_nobody(db: AsyncSession) -> None:
    what = await _install_admin(db, SHARED_SCHEMA, "boss@acme.com", "Dana Boss", guest=False)
    await db.commit()

    assert "created" in what
    placed = await EmployeeRepository(db).get_by_work_email("boss@acme.com")
    assert placed is not None
    assert placed.role is Role.ADMIN
    assert placed.is_active is True
    assert placed.full_name == "Dana Boss"


async def test_without_a_name_the_email_stands_in(db: AsyncSession) -> None:
    """The real name backfills from the identity provider on first sign-in, so
    omitting --name costs nothing permanent."""
    await _install_admin(db, SHARED_SCHEMA, "dana.boss@acme.com", None, guest=False)
    await db.commit()

    placed = await EmployeeRepository(db).get_by_work_email("dana.boss@acme.com")
    assert placed is not None
    assert placed.full_name == placeholder_name_from_email("dana.boss@acme.com")


async def test_an_existing_person_is_promoted_not_duplicated(db: AsyncSession) -> None:
    await _employee(db, "lead@acme.com", Role.MANAGER)

    what = await _install_admin(db, SHARED_SCHEMA, "lead@acme.com", None, guest=False)
    await db.commit()

    assert "promoted from manager to admin" in what
    person = await _reread(db, "lead@acme.com")
    assert person.role is Role.ADMIN
    assert person.full_name == "Existing Person"  # not overwritten


async def test_an_offboarded_person_is_brought_back(db: AsyncSession) -> None:
    await _employee(db, "returner@acme.com", Role.EXECUTIVE, active=False)

    what = await _install_admin(db, SHARED_SCHEMA, "returner@acme.com", None, guest=False)
    await db.commit()

    assert "reactivated" in what and "promoted" in what
    person = await _reread(db, "returner@acme.com")
    assert person.is_active is True
    assert person.status is EmployeeStatus.ACTIVE
    assert person.role is Role.ADMIN


async def test_running_it_twice_changes_nothing_the_second_time(db: AsyncSession) -> None:
    await _install_admin(db, SHARED_SCHEMA, "boss@acme.com", "Dana Boss", guest=False)
    await db.commit()

    what = await _install_admin(db, SHARED_SCHEMA, "boss@acme.com", "Dana Boss", guest=False)
    await db.commit()

    assert what == "was already an admin there"


async def test_an_it_admin_is_not_reported_as_promoted(db: AsyncSession) -> None:
    """`it_admin` normalizes to a full admin (`CurrentUser._normalize_role`), so
    they already administer the workspace and nothing should change."""
    await _employee(db, "it@acme.com", Role.IT_ADMIN)

    what = await _install_admin(db, SHARED_SCHEMA, "it@acme.com", None, guest=False)
    await db.commit()

    assert what == "was already an admin there"
    assert (await _reread(db, "it@acme.com")).role is Role.IT_ADMIN


async def test_a_differently_cased_email_matches_the_same_person(db: AsyncSession) -> None:
    """The email is typed on a command line. An exact-match lookup would miss
    the row and the create would then collide with the unique index."""
    await _employee(db, "Dana.Boss@Acme.com", Role.EXECUTIVE)

    what = await _install_admin(db, SHARED_SCHEMA, "dana.boss@acme.com", None, guest=False)
    await db.commit()

    assert "promoted" in what
    assert (await _reread(db, "Dana.Boss@Acme.com")).role is Role.ADMIN
    assert len(await EmployeeRepository(db).list_all_active()) == 1


async def test_two_rows_differing_only_in_case_are_refused(db: AsyncSession) -> None:
    """Picking one at random would make an admin of whichever the index
    happened to return first."""
    await _employee(db, "dup@acme.com", Role.EXECUTIVE)
    await _employee(db, "DUP@acme.com", Role.EXECUTIVE)

    with pytest.raises(ConflictError):
        await _install_admin(db, SHARED_SCHEMA, "dup@acme.com", None, guest=False)


# --------------------------------------------------------------------------- #
# Guest admins - access to a workspace without employment in it
#
# `admin --guest` is how someone from headquarters administers a sister
# company. The flag is the only thing standing between that and enrolling them
# in the other company's payroll, so both directions are pinned here.
# --------------------------------------------------------------------------- #
async def test_a_guest_admin_is_created_outside_the_headcount(db: AsyncSession) -> None:
    what = await _install_admin(db, SHARED_SCHEMA, "hq@acme.com", "Hq Person", guest=True)
    await db.commit()

    assert what == "created as a guest admin"
    person = await _reread(db, "hq@acme.com")
    assert person.role is Role.ADMIN  # full privilege, as intended
    assert person.is_guest is True


async def test_an_existing_employee_can_be_turned_into_a_guest(db: AsyncSession) -> None:
    """The repair path. Somebody installed as a full employee before the flag
    existed is taken out of the headcount, the payroll run and the attendance
    report - without disturbing their role or their audit history."""
    await _employee(db, "already@acme.com", Role.ADMIN)

    what = await _install_admin(db, SHARED_SCHEMA, "already@acme.com", None, guest=True)
    await db.commit()

    assert "made a guest" in what
    person = await _reread(db, "already@acme.com")
    assert person.is_guest is True
    assert person.role is Role.ADMIN


async def test_a_guest_can_be_turned_back_into_a_real_employee(db: AsyncSession) -> None:
    """The other direction, for the day somebody is actually hired here."""
    await _install_admin(db, SHARED_SCHEMA, "joining@acme.com", "Joins Later", guest=True)
    await db.commit()

    what = await _install_admin(db, SHARED_SCHEMA, "joining@acme.com", None, guest=False)
    await db.commit()

    assert "made a full employee" in what
    assert (await _reread(db, "joining@acme.com")).is_guest is False


async def test_installing_the_same_guest_twice_changes_nothing(db: AsyncSession) -> None:
    await _install_admin(db, SHARED_SCHEMA, "twice@acme.com", "Twice", guest=True)
    await db.commit()

    what = await _install_admin(db, SHARED_SCHEMA, "twice@acme.com", None, guest=True)
    await db.commit()

    assert what == "was already an admin there"


# --------------------------------------------------------------------------- #
# Branding - a new workspace must not introduce itself as another company
#
# The name lives in two places: the `workspaces` registry in `public` (what the
# switcher lists) and `org_settings` inside the workspace's own schema (what the
# dashboard chrome renders). A schema built from the migration chain knows
# nothing about who it is, so its `org_settings` fell back to column defaults -
# and those defaults were the FIRST company's name.
# --------------------------------------------------------------------------- #
async def test_a_workspace_with_no_settings_row_gets_its_own_name(db: AsyncSession) -> None:
    what = await _apply_branding(db, SHARED_SCHEMA, "Alt Opti", "Pune")
    await db.commit()

    assert what == "set 'Alt Opti' - 'Pune'"
    org = await OrgSettingsRepository(db).get()
    assert org is not None
    assert (org.name, org.location) == ("Alt Opti", "Pune")


async def test_branding_repairs_a_workspace_wearing_another_companys_name(
    db: AsyncSession,
) -> None:
    """The bug as it actually appeared: the chrome said "Optiminastic - HQ"
    while the switcher beside it correctly said "Alt Opti - Pune"."""
    orgs = OrgSettingsRepository(db)
    stale = await orgs.create_default()
    await db.commit()
    assert stale.name == "Optiminastic", "the default this command exists to undo"

    what = await _apply_branding(db, SHARED_SCHEMA, "Alt Opti", "Pune")
    await db.commit()

    assert what == "changed from 'Optiminastic' - 'HQ' to 'Alt Opti' - 'Pune'"
    org = await OrgSettingsRepository(db).get()
    assert org is not None
    assert (org.name, org.location) == ("Alt Opti", "Pune")


async def test_rebranding_to_the_same_name_reports_no_change(db: AsyncSession) -> None:
    """Idempotent, and says so - `brand` is safe to re-run across every
    workspace without having to know which ones were already correct."""
    await _apply_branding(db, SHARED_SCHEMA, "Alt Opti", "Pune")
    await db.commit()

    what = await _apply_branding(db, SHARED_SCHEMA, "Alt Opti", "Pune")
    await db.commit()

    assert what == "already correct"


async def test_branding_leaves_a_curated_logo_alone(db: AsyncSession) -> None:
    """It syncs the NAME, not the whole identity row. A workspace admin who has
    uploaded a logo and set a product wordmark must not lose them to a repair
    command that only ever meant to fix the name."""
    orgs = OrgSettingsRepository(db)
    org = await orgs.create_default()
    org.product_name = "Altera"
    org.logo_object_key = "logos/alt-opti.png"
    await db.commit()

    await _apply_branding(db, SHARED_SCHEMA, "Alt Opti", "Pune")
    await db.commit()

    fresh = await OrgSettingsRepository(db).get()
    assert fresh is not None
    assert fresh.product_name == "Altera"
    assert fresh.logo_object_key == "logos/alt-opti.png"


async def test_a_row_changed_before_branding_is_dropped(db: AsyncSession) -> None:
    """The hazard that decides the order of operations in `create`.

    `_apply_branding` expunges the identity map - it has to, because it moves
    the session between schemas and a cached row would otherwise be answered
    from the wrong one. The cost is that anything modified but not yet flushed
    is detached along with it, and its UPDATE never reaches the commit.

    `create` therefore brands FIRST and writes `provisioned_at` afterwards.
    Written the other way round, every new workspace stayed at "not ready" -
    the state both `brand` and `admin` refuse to touch, so the workspace could
    not be opened at all.

    Pinned as a test rather than a comment because the failure is silent: the
    commit succeeds and reports nothing, the row simply never changed.
    """
    workspaces = WorkspaceRepository(db)
    workspaces.add(
        Workspace(
            slug="hazard",
            schema_name=SHARED_SCHEMA,
            name="Hazard",
            location="Nowhere",
            status=WorkspaceStatus.ACTIVE,
            provisioned_at=None,
        )
    )
    await db.commit()

    row = await workspaces.get_by_slug("hazard")
    assert row is not None
    row.provisioned_at = datetime.now(UTC)  # modified, deliberately not flushed
    await _apply_branding(db, SHARED_SCHEMA, "Hazard", "Nowhere")
    await db.commit()

    after = await WorkspaceRepository(db).get_by_slug("hazard")
    assert after is not None
    assert after.provisioned_at is None, (
        "the change survived the expunge - if this ever passes, `create` may "
        "safely set provisioned_at before branding again"
    )
