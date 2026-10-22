"""Which workspace a request is looking at, and how that is enforced.

Avora is one application serving several workspaces. Each workspace's tables
live in their own Postgres **schema**; a request sets `search_path` to that
schema, and every existing query - all 237 of them - then reads and writes that
workspace's rows without being changed at all.

That is the whole point of doing it this way. The alternative, a `workspace_id`
column filtered in every query, makes isolation something 57 repositories have
to REMEMBER. Here it is something the connection ENFORCES: a query that forgets
the workspace cannot exist, because there is nothing to forget.

**The first workspace keeps `public`.** Its tables are already there, and moving
69 live tables holding payroll and attendance would be the single most dangerous
step in this whole change for no gain. So a workspace's schema is recorded on
its row rather than derived from its name: the original is `public`, every new
one is `ws_<slug>`.

Two tables live outside every workspace, in `public` alongside the first
workspace's data: the workspace list and the membership registry
(`app/models/workspace.py`). They are the address book that answers "which
workspaces may this email enter", which has to be answerable before a workspace
is chosen. Better Auth's tables (Prisma, from the Next.js app) are there too -
they are identity, not workspace data, so one sign-in maps to an employee row in
each workspace that person belongs to.
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

# A slug becomes part of a schema name, which cannot be a bound parameter in
# `SET search_path`. So it is constrained to characters that are safe by
# construction and validated on every use - never trusted from a request.
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,38}$")

SCHEMA_PREFIX = "ws_"
# Where the address book, Better Auth, and the first workspace's tables live.
SHARED_SCHEMA = "public"


class InvalidWorkspaceSlug(ValueError):
    """A slug or schema name that could not safely be used."""


def validate_slug(slug: str) -> str:
    """The slug, or a refusal.

    Deliberately strict: lowercase letters, digits and underscores, starting
    with a letter. Anything else is rejected rather than sanitised, because a
    "cleaned" slug could silently collide with another workspace's schema.
    """
    candidate = slug.strip().lower()
    if not SLUG_PATTERN.match(candidate):
        raise InvalidWorkspaceSlug(
            "A workspace key must be 2-39 characters: lowercase letters, digits "
            "and underscores, starting with a letter."
        )
    return candidate


def new_schema_for(slug: str) -> str:
    """The schema a NEWLY created workspace gets. The first workspace does not
    go through here - it keeps `public`."""
    return f"{SCHEMA_PREFIX}{validate_slug(slug)}"


def validate_schema_name(schema: str) -> str:
    """A schema name read back from a workspace row, checked before it is put
    into SQL. Rows are written by this app, but a value that reaches `SET` is
    worth re-checking however it got there."""
    candidate = schema.strip().lower()
    if candidate == SHARED_SCHEMA:
        return candidate
    slug = candidate[len(SCHEMA_PREFIX) :] if candidate.startswith(SCHEMA_PREFIX) else ""
    if not slug or not SLUG_PATTERN.match(slug):
        raise InvalidWorkspaceSlug(f"{schema!r} is not a workspace schema.")
    return candidate


def _supports_schemas(session: AsyncSession) -> bool:
    """Whether this database has schemas to point at.

    Postgres does; the SQLite the test suite runs on does not, and there
    everything lives in one namespace - which is precisely what a
    single-workspace install looks like. So the schema calls become no-ops
    rather than the suite needing a Postgres container to test anything.
    """
    return session.get_bind().dialect.name == "postgresql"


async def use_schema(session: AsyncSession, schema: str) -> None:
    """Point this session's connection at one workspace, for its whole life.

    `public` stays on the path AFTER the workspace schema, so the address book
    stays reachable and a workspace's own table of a given name always wins. A
    workspace can never be served another's rows by accident.

    The name is interpolated because Postgres will not accept a bound parameter
    in SET, which is exactly why `validate_schema_name` is strict and why every
    path into here goes through it.
    """
    name = validate_schema_name(schema)
    if not _supports_schemas(session):
        return
    if name == SHARED_SCHEMA:
        await session.execute(text(f"SET search_path TO {SHARED_SCHEMA}"))
        return
    await session.execute(text(f'SET search_path TO "{name}", {SHARED_SCHEMA}'))


async def use_shared_only(session: AsyncSession) -> None:
    """Point a session at `public` alone - for the address book, before any
    workspace has been resolved."""
    if not _supports_schemas(session):
        return
    await session.execute(text(f"SET search_path TO {SHARED_SCHEMA}"))


async def create_schema(session: AsyncSession, schema: str) -> None:
    """Make a new workspace's schema. Does NOT create its tables: the migration
    runner does that, so a new workspace is built by exactly the same migrations
    every other workspace was."""
    name = validate_schema_name(schema)
    if name == SHARED_SCHEMA or not _supports_schemas(session):
        return
    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{name}"'))


async def list_workspace_schemas(session: AsyncSession) -> list[str]:
    """Every `ws_` schema in the database, read from Postgres rather than from
    the workspace table - the migration runner must reach a schema even if its
    row is missing or the app cannot boot."""
    rows = await session.execute(
        text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE :prefix ORDER BY schema_name"
        ),
        {"prefix": f"{SCHEMA_PREFIX}%"},
    )
    out: list[str] = []
    for (name,) in rows.all():
        try:
            out.append(validate_schema_name(name))
        except InvalidWorkspaceSlug:
            continue  # someone else's schema that happens to start with ws_
    return out
