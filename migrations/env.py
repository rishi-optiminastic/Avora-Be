"""Alembic migration environment (async).

The DB URL and target metadata come from the app, so migrations and the running
service can never drift. We never call create_all in any non-test environment
(Database conventions §8) - schema changes flow through these migrations only.

**Multi-workspace.** Each workspace's tables live in its own Postgres schema
(`ws_<slug>`), and this chain builds ONE of them per run. Which one comes from
`-x schema=ws_foo`; without it the default `search_path` applies, which is how
a single-workspace database and the test suite still work unchanged.

Every workspace schema keeps its OWN `alembic_version`, so workspaces can be at
different revisions and one failing to migrate does not mark the others done.
`scripts/tenancy.py migrate` is what walks them all; running plain
`alembic upgrade head` migrates one schema, never all of them.

The two SHARED tables (`public.workspaces`, `public.workspace_members`) are
excluded here and created by `scripts/tenancy.py bootstrap`. They are not part
of any workspace, so they must not be created once per workspace.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(get_settings().database_url))
target_metadata = Base.metadata

# Tables owned by Better Auth (Prisma, in the Next.js app) live in the same DB
# but are NOT part of this backend's metadata. Exclude them so autogenerate
# never proposes dropping or altering another tool's tables.
_PRISMA_TABLES = frozenset(
    {"auth_user", "auth_session", "auth_account", "auth_verification", "auth_jwks"}
)

# Shared across every workspace, and owned by `scripts/tenancy.py`, not by this
# chain. Identified by their explicit `public` schema rather than by name, so a
# future shared table is excluded automatically.
_SHARED_SCHEMA = "public"

# The workspace schema this run targets: `alembic -x schema=ws_foo upgrade head`.
_TARGET_SCHEMA = context.get_x_argument(as_dictionary=True).get("schema")


def _is_shared(obj: object) -> bool:
    schema = getattr(obj, "schema", None)
    return schema == _SHARED_SCHEMA


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    if type_ == "table":
        return name not in _PRISMA_TABLES and not _is_shared(obj)
    parent_table = getattr(obj, "table", None)
    if parent_table is not None:
        if parent_table.name in _PRISMA_TABLES or _is_shared(parent_table):
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    if _TARGET_SCHEMA:
        # Point the connection at this workspace before anything runs, so every
        # unqualified CREATE / ALTER in ~100 existing migrations lands in it
        # without one of them being edited. `public` stays on the path for the
        # shared tables and for types the migrations reference.
        connection.exec_driver_sql(f'SET search_path TO "{_TARGET_SCHEMA}", public')
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        # Each workspace tracks its own revision. Without this they would share
        # public.alembic_version and the second workspace would be told it was
        # already up to date with no tables at all.
        version_table_schema=_TARGET_SCHEMA or None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
