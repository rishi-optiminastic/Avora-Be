"""Workspace operations: bootstrap, create, migrate, grant.

Avora serves several workspaces from one deployment. Each one's 69 tables live
in its own Postgres schema; the first keeps `public`, where its data already is.
This script owns everything that happens OUTSIDE a single request:

    uv run python -m scripts.tenancy bootstrap
    uv run python -m scripts.tenancy list
    uv run python -m scripts.tenancy create acme --name "Acme" --location "Pune"
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
from app.core.tenancy import (
    SHARED_SCHEMA,
    InvalidWorkspaceSlug,
    create_schema,
    list_workspace_schemas,
    new_schema_for,
    use_shared_only,
    validate_schema_name,
    validate_slug,
)
from app.db.session import SessionFactory, engine
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.workspace import WorkspaceRepository, normalize_email


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

    _say(f"workspace {slug!r} is ready. Grant someone access:")
    _say(f"  uv run python -m scripts.tenancy grant {slug} you@company.com")
    return 0


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
