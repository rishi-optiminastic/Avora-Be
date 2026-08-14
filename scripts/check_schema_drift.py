#!/usr/bin/env python3
"""Report how far a deployed database has drifted from the code's expectations.

Read-only. Answers, in one shot, the questions that otherwise take a round-trip
each: which migration the DB is on, how many the code has beyond that, whether
the enum values the code writes actually exist, and whether the columns it reads
are present.

Written because a production 500 on "post a task comment" could not be told apart
from three different candidate causes without this. Postgres aborts a whole
transaction on one bad statement, so a single missing enum value takes down the
user's write — and the generic error handler (correctly) refuses to say which.

Usage, from be/:

    uv run python scripts/check_schema_drift.py                  # uses DATABASE_URL
    uv run python scripts/check_schema_drift.py <database-url>   # or an explicit one

Run it against the DEPLOYED database, not your local one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Enum values the code writes but older databases may not carry. Each is added by
# an `ALTER TYPE ... ADD VALUE` migration, which is invisible to a table-shape
# diff — the usual reason a deploy "looks" fine and still 500s.
_REQUIRED_ENUM_VALUES: dict[str, str] = {
    "notificationkind": "task_comment",
}

# (table, column) pairs whose absence breaks a specific user-facing action.
_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_comments", "idempotency_key"),
    ("notifications", "entity_id"),
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _normalise(url: str) -> str:
    """Force the asyncpg driver — the app's URL may name any dialect."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith(("postgresql://", "postgres://")):
        return url.replace("postgres://", "postgresql://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    return url


def _code_revisions() -> tuple[str, list[str]]:
    """(head revision, every revision newest-first) as the code defines them."""
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head() or "?"
    return head, [rev.revision for rev in script.walk_revisions()]


async def _report(url: str) -> int:
    """Print the drift report. Returns a process exit code (0 = at head)."""
    engine = create_async_engine(_normalise(url))
    problems: list[str] = []
    try:
        async with engine.connect() as conn:
            db_revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            head, revisions = _code_revisions()

            print(f"DB revision   : {db_revision}")
            print(f"Code head     : {head}")
            if db_revision == head:
                print("Status        : at head ✓")
            elif db_revision in revisions:
                behind = revisions.index(db_revision)
                print(f"Status        : BEHIND by {behind} migration(s) ✗")
                problems.append(f"{behind} unapplied migration(s)")
            else:
                print("Status        : UNKNOWN revision — DB is not on this code's history ✗")
                problems.append("unknown revision")

            print()
            for enum_name, required in _REQUIRED_ENUM_VALUES.items():
                values = list(
                    await conn.scalars(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e "
                            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
                        ),
                        {"name": enum_name},
                    )
                )
                if not values:
                    print(f"enum {enum_name}: MISSING TYPE ✗")
                    problems.append(f"enum {enum_name} absent")
                elif required in values:
                    print(f"enum {enum_name}: has '{required}' ✓")
                else:
                    print(f"enum {enum_name}: MISSING '{required}' ✗  (has: {', '.join(values)})")
                    problems.append(f"{enum_name} lacks '{required}'")

            print()
            for table, column in _REQUIRED_COLUMNS:
                present = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                )
                mark = "✓" if present else "✗"
                print(f"{table}.{column}: {'present' if present else 'MISSING'} {mark}")
                if not present:
                    problems.append(f"{table}.{column} missing")
    finally:
        await engine.dispose()

    print()
    if problems:
        print("DRIFT FOUND: " + "; ".join(problems))
        print("Fix: run `alembic upgrade head` against this database.")
        return 1
    print("No drift — schema matches the code.")
    return 0


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("Pass a database URL, or set DATABASE_URL.")
    raise SystemExit(asyncio.run(_report(url)))


if __name__ == "__main__":
    main()
