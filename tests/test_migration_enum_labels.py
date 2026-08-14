"""Migration enum labels must match the Python enum MEMBER NAMES.

SQLAlchemy persists an Enum column by member name, not value — `LeaveType.SICK`
is written as `SICK`. A migration that creates or extends a Postgres enum with
lowercase labels therefore produces a type the ORM can never write to, and the
failure only appears at runtime against real Postgres.

That is not hypothetical: two migrations shipped lowercase labels and took down
task comments and every leave page respectively. The test suite could not catch
it because the schema is built with `create_all` on SQLite, where enums are plain
text and casing is irrelevant — so migrations get no coverage at all.

This closes that specific gap cheaply: it reads the migration files as text and
checks the labels, no database required.
"""

from __future__ import annotations

import re
from pathlib import Path

_VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"

# `ALTER TYPE <name> ADD VALUE [IF NOT EXISTS] '<label>'` — the literal form. The
# f-string loop variants (`'{value}'`) are skipped: the label isn't a literal
# there, and those loops are fed from tuples this test also sees as literals.
_ADD_VALUE = re.compile(r"ADD VALUE(?:\s+IF NOT EXISTS)?\s+'([^']+)'", re.IGNORECASE)


def _migration_files() -> list[Path]:
    return sorted(_VERSIONS.glob("*.py"))


def test_migration_files_exist() -> None:
    """Guard the guard — a bad glob would make every assertion below vacuous."""
    assert len(_migration_files()) > 10


def test_enum_labels_added_by_migrations_are_member_names() -> None:
    """Every literal ADD VALUE label must be uppercase.

    Member names in this codebase are SCREAMING_SNAKE_CASE, which is what
    SQLAlchemy writes. A lowercase label is dead on arrival.
    """
    offenders: list[str] = []
    for path in _migration_files():
        for label in _ADD_VALUE.findall(path.read_text(encoding="utf-8")):
            if "{" in label:  # an f-string placeholder, checked at its source
                continue
            if label != label.upper():
                offenders.append(f"{path.name}: '{label}' should be '{label.upper()}'")
    assert not offenders, "Enum labels must be member names (uppercase):\n" + "\n".join(offenders)


def test_declared_enum_types_use_uppercase_labels() -> None:
    """Same rule for `sa.Enum("a", "b", name=...)` type definitions.

    Catches the create-a-new-type case, which is how the tenure band enum broke —
    the type was created lowercase and the first insert failed.
    """
    pattern = re.compile(r"sa\.Enum\(\s*((?:\s*\"[^\"]+\",\s*)+)\s*name=", re.MULTILINE)
    offenders: list[str] = []
    for path in _migration_files():
        for block in pattern.findall(path.read_text(encoding="utf-8")):
            for label in re.findall(r'"([^"]+)"', block):
                if label != label.upper():
                    offenders.append(f"{path.name}: '{label}' should be '{label.upper()}'")
    assert not offenders, "Enum type labels must be member names (uppercase):\n" + "\n".join(
        offenders
    )
