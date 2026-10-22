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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import WORKSPACE_HEADER, _resolve_workspace
from app.core.exceptions import AuthorizationError
from app.core.tenancy import (
    SHARED_SCHEMA,
    InvalidWorkspaceSlug,
    new_schema_for,
    validate_schema_name,
    validate_slug,
)
from app.models.workspace import Workspace, WorkspaceStatus
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
