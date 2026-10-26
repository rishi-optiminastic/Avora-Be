"""Workspace operations: bootstrap, create, migrate, grant.

Avora serves several workspaces from one deployment. Each one's 69 tables live
in its own Postgres schema; the first keeps `public`, where its data already is.
This script owns everything that happens OUTSIDE a single request:

    uv run python -m scripts.tenancy bootstrap
    uv run python -m scripts.tenancy list
    uv run python -m scripts.tenancy create acme --name "Acme" --location "Pune"
    uv run python -m scripts.tenancy admin acme boss@acme.com --name "Dana Boss"
    uv run python -m scripts.tenancy migrate
    uv run python -m scripts.tenancy grant acme someone@corp.com
    uv run python -m scripts.tenancy revoke acme someone@corp.com

**Why a script and not a migration.** Creating a workspace runs the whole
Alembic chain against a brand new schema, which cannot happen from inside a
migration. And `migrate` has to walk every schema: plain `alembic upgrade head`
moves exactly one, so a deploy hook that calls it would leave every workspace
but the first on an old schema - silently, which is the failure this deployment
has already had once (see the Coolify pre-deploy note in the README).

Nothing here is reachable from the API. Creating a workspace is a deliberate,
auditable act by whoever runs the deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.core.names import clean_display_name, placeholder_name_from_email
from app.core.tenancy import (
    SHARED_SCHEMA,
    InvalidWorkspaceSlug,
    create_schema,
    list_workspace_schemas,
    new_schema_for,
    use_schema,
    use_shared_only,
    validate_schema_name,
    validate_slug,
)
from app.db.session import SessionFactory, engine
from app.models.employee import EmployeeStatus, Role
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.employee import EmployeeRepository
from app.repositories.workspace import WorkspaceRepository, normalize_email

# `it_admin` is a full admin everywhere (`CurrentUser._normalize_role`), so
# somebody holding it is already an admin of their workspace and must not be
# reported as "promoted".
ADMIN_ROLES = (Role.ADMIN, Role.IT_ADMIN)


def _say(message: str) -> None:
    print(message, flush=True)


async def _shared_session() -> AsyncSession:
    """A session pinned to `public`, where the address book lives."""
    session = SessionFactory()
    await use_shared_only(session)
    return session


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
async def bootstrap() -> int:
    """Create the two shared tables and register the existing data as the first
    workspace.

    Deliberately moves NOTHING. The 28 employees, their payroll and their
    attendance stay in `public` exactly where they are; the workspace row simply
    records that `public` is where workspace one lives. Nothing is at risk and
    there is no downtime.

    Safe to run twice.
    """
    settings = get_settings()
    slug = validate_slug(settings.primary_workspace_slug)

    async with engine.begin() as conn:
        # Only the two shared tables - the 69 workspace tables are Alembic's.
        shared = [
            Workspace.metadata.tables["public.workspaces"],
            Workspace.metadata.tables["public.workspace_members"],
        ]
        await conn.run_sync(Workspace.metadata.create_all, tables=shared, checkfirst=True)
    _say("shared tables ready (public.workspaces, public.workspace_members)")

    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        existing = await workspaces.get_by_slug(slug)
        if existing is not None:
            _say(f"workspace {slug!r} already registered -> {existing.schema_name}")
            await session.commit()
            return 0
        workspace = Workspace(
            slug=slug,
            schema_name=SHARED_SCHEMA,
            name=settings.primary_workspace_slug.replace("_", " ").title(),
            location="",
            status=WorkspaceStatus.ACTIVE,
            # Already built, by every migration that has ever run here.
            provisioned_at=datetime.now(UTC),
        )
        workspaces.add(workspace)
        await session.commit()
        _say(f"registered {slug!r} -> {SHARED_SCHEMA} (existing data, untouched)")
        return 0
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def _alembic_upgrade(schema: str) -> bool:
    """Run the whole chain against one schema, in a subprocess.

    A subprocess because Alembic configures global state from `migrations/env.py`
    and running it twice in one process shares that state. Each workspace gets a
    clean run, which is also what makes a failure isolated to one workspace.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-x", f"schema={schema}", "upgrade", "head"],
        check=False,
    )
    return result.returncode == 0


async def create(slug: str, name: str, location: str) -> int:
    settings = get_settings()
    slug = validate_slug(slug)
    if slug == validate_slug(settings.primary_workspace_slug):
        _say(f"{slug!r} is the first workspace; run bootstrap instead.")
        return 1

    schema = new_schema_for(slug)
    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        if await workspaces.get_by_slug(slug) is not None:
            _say(f"workspace {slug!r} already exists.")
            return 1
        await create_schema(session, schema)
        # Registered BEFORE the migrations run, with provisioned_at unset: if
        # the chain fails halfway the row shows a workspace that exists but
        # cannot be entered, which is the honest state. `migrate` finishes it.
        workspaces.add(
            Workspace(
                slug=slug,
                schema_name=schema,
                name=name,
                location=location,
                status=WorkspaceStatus.ACTIVE,
                provisioned_at=None,
            )
        )
        await session.commit()
        _say(f"created schema {schema}")
    finally:
        await session.close()

    _say(f"building {slug!r} from the migration chain...")
    if not _alembic_upgrade(schema):
        _say(f"migrations FAILED for {schema}. The workspace stays unusable; fix and re-run.")
        return 1

    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_by_slug(slug)
        if workspace is None:  # pragma: no cover - just written above
            return 1
        workspace.provisioned_at = datetime.now(UTC)
        await session.commit()
    finally:
        await session.close()

    _say(f"workspace {slug!r} is ready, and EMPTY - it has no employees at all.")
    _say("Give it its first admin (this is the step that makes it openable):")
    _say(f'  uv run python -m scripts.tenancy admin {slug} you@company.com --name "Your Name"')
    return 0


# --------------------------------------------------------------------------- #
# admin - the first person inside a new workspace
# --------------------------------------------------------------------------- #
async def _install_admin(session: AsyncSession, schema: str, email: str, name: str | None) -> str:
    """Make one person an ADMIN EMPLOYEE inside a workspace, and say what changed.

    The session is pointed at that workspace's own schema for the length of
    this and put back afterwards, because the row has to be written where that
    workspace's employees live rather than in `public` with the address book.
    `expunge_all` either side for the reason `_place_in_target` does it: the
    identity map is full of rows read from the other schema, and a lookup after
    the switch would otherwise be answered from the wrong workspace's cache.
    """
    address = normalize_email(email)
    session.expunge_all()
    await use_schema(session, schema)
    try:
        employees = EmployeeRepository(session)
        # Insensitive, because the email is typed on a command line and the row
        # may have been written with different capitalisation. The exact lookup
        # would miss it and the create below would then hit the unique index.
        existing = await employees.find_by_work_email_insensitive(address)
        if existing is None:
            # The real name backfills from the identity provider on first
            # sign-in (`refresh_display_name`), so omitting --name costs
            # nothing permanent.
            await employees.create_direct(
                work_email=address,
                full_name=clean_display_name(name) or placeholder_name_from_email(address),
                role=Role.ADMIN,
                profile={},
            )
            return "created as an admin"

        changed: list[str] = []
        if not existing.is_active:
            existing.is_active = True
            existing.status = EmployeeStatus.ACTIVE
            changed.append("reactivated")
        if existing.role not in ADMIN_ROLES:
            changed.append(f"promoted from {existing.role.value} to admin")
            existing.role = Role.ADMIN
        await session.flush()
        return " and ".join(changed) if changed else "was already an admin there"
    finally:
        # Always back, even on failure - the grant below is written in `public`.
        session.expunge_all()
        await use_shared_only(session)


async def admin(slug: str, email: str, name: str | None) -> int:
    """Install a workspace's first admin.

    This is the one act that cannot be done from inside the app, and without it
    a new workspace is unreachable forever. `grant` alone is not enough: it
    writes a membership row, so the workspace appears in the switcher, but
    every workspace keeps its OWN employees and `get_current_user` refuses
    anyone without an active employee row in the schema it landed in. Clicking
    the new workspace then 401s, the dashboard treats that as a stale choice
    and clears the cookie, and the person is bounced straight back to the
    workspace they started in with nothing said.

    Granting membership here too, so one command makes the workspace openable.
    """
    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_by_slug(validate_slug(slug))
        if workspace is None:
            _say(f"no workspace {slug!r}")
            return 1
        if workspace.provisioned_at is None:
            _say(f"{workspace.slug!r} has not finished building - run migrate first.")
            return 1
        # Read off the row BEFORE `_install_admin` expunges it.
        workspace_id, label = workspace.id, workspace.slug
        schema = validate_schema_name(workspace.schema_name)

        try:
            what = await _install_admin(session, schema, email, name)
        except ConflictError as exc:
            # Two rows differing only in case: picking one at random would make
            # an admin of whichever the index happened to return.
            _say(f"cannot do this safely: {exc}")
            return 1
        await workspaces.grant(workspace_id, email, granted_by_email="cli")
        await session.commit()

        _say(f"{normalize_email(email)} {what} in {label!r} ({schema})")
        _say(f"Sign in and pick {label!r} from the workspace menu.")
        return 0
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #
async def migrate() -> int:
    """Bring every workspace to head, the first one included.

    This is what a deploy should run. One workspace failing does not stop the
    others, and the exit code is non-zero if any failed, so a deploy that
    half-migrated is visibly a failure rather than a quiet one.
    """
    session = await _shared_session()
    try:
        schemas = [SHARED_SCHEMA, *await list_workspace_schemas(session)]
    finally:
        await session.close()

    failures: list[str] = []
    for schema in schemas:
        _say(f"--- migrating {schema} ---")
        # The first workspace is `public`, which is also the default search
        # path, so it runs without -x and keeps its existing alembic_version.
        ok = _alembic_upgrade_default() if schema == SHARED_SCHEMA else _alembic_upgrade(schema)
        if not ok:
            failures.append(schema)
    if failures:
        _say(f"FAILED: {', '.join(failures)}")
        return 1
    _say(f"all {len(schemas)} workspace schema(s) at head")
    return 0


def _alembic_upgrade_default() -> bool:
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=False)
    return result.returncode == 0


# --------------------------------------------------------------------------- #
# list / grant / revoke
# --------------------------------------------------------------------------- #
async def show() -> int:
    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        rows = await workspaces.list_all()
        if not rows:
            _say("no workspaces registered - run bootstrap")
            return 0
        for workspace in rows:
            revision = await _revision_of(session, workspace.schema_name)
            people = await workspaces.member_count(workspace.id)
            ready = "ready" if workspace.provisioned_at else "NOT PROVISIONED"
            _say(
                f"{workspace.slug:<20} {workspace.schema_name:<24} "
                f"{workspace.status.value:<10} {ready:<16} "
                f"{people} member(s)  rev={revision or '-'}"
            )
        return 0
    finally:
        await session.close()


async def _revision_of(session: AsyncSession, schema: str) -> str | None:
    """The Alembic revision a schema is at, or None if it has none yet.

    A table name cannot be a bound parameter, so the schema is validated first -
    it comes from a workspace row, and anything reaching SQL is re-checked
    however it got there. The existence check is a bound query, so the only
    interpolated value is one `validate_schema_name` has already approved.
    """
    name = validate_schema_name(schema)
    exists = await session.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = 'alembic_version'"
        ),
        {"schema": name},
    )
    if exists.first() is None:
        return None
    found = await session.execute(
        text(f'SELECT version_num FROM "{name}".alembic_version')  # noqa: S608
    )
    row = found.first()
    return str(row[0]) if row else None


async def grant(slug: str, email: str) -> int:
    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_by_slug(validate_slug(slug))
        if workspace is None:
            _say(f"no workspace {slug!r}")
            return 1
        await workspaces.grant(workspace.id, email, granted_by_email="cli")
        await session.commit()
        _say(f"{normalize_email(email)} can now open {workspace.slug!r}")
        return 0
    finally:
        await session.close()


async def revoke(slug: str, email: str) -> int:
    session = await _shared_session()
    try:
        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_by_slug(validate_slug(slug))
        if workspace is None:
            _say(f"no workspace {slug!r}")
            return 1
        await workspaces.revoke(workspace.id, email)
        await session.commit()
        _say(f"{normalize_email(email)} can no longer open {workspace.slug!r}")
        return 0
    finally:
        await session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Avora workspace operations.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="register the existing data as workspace one")
    sub.add_parser("list", help="show every workspace and its revision")
    sub.add_parser("migrate", help="bring every workspace to head")

    create_cmd = sub.add_parser("create", help="create and build a new workspace")
    create_cmd.add_argument("slug")
    create_cmd.add_argument("--name", required=True)
    create_cmd.add_argument("--location", default="")

    admin_cmd = sub.add_parser("admin", help="install a workspace's first admin employee")
    admin_cmd.add_argument("slug")
    admin_cmd.add_argument("email")
    admin_cmd.add_argument("--name", default=None, help="display name; defaults to the email")

    for verb in ("grant", "revoke"):
        access = sub.add_parser(verb, help=f"{verb} one person's access to a workspace")
        access.add_argument("slug")
        access.add_argument("email")

    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            return asyncio.run(bootstrap())
        if args.command == "list":
            return asyncio.run(show())
        if args.command == "migrate":
            return asyncio.run(migrate())
        if args.command == "create":
            return asyncio.run(create(args.slug, args.name, args.location))
        if args.command == "admin":
            return asyncio.run(admin(args.slug, args.email, args.name))
        if args.command == "grant":
            return asyncio.run(grant(args.slug, args.email))
        if args.command == "revoke":
            return asyncio.run(revoke(args.slug, args.email))
    except InvalidWorkspaceSlug as exc:
        _say(str(exc))
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
