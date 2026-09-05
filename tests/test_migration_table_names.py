"""Migrations are checked here because nothing else checks them.

Tests build the schema with `create_all` from the ORM, so a migration is never
executed by the suite — a wrong table name, or a comparison against an enum
column without a cast, passes everything and then takes the deploy down. Both
have happened.

Tests build the schema with `create_all` from the ORM, so a migration is never
executed here — a wrong table name passes every test and then takes the deploy
down. That is exactly what happened: `audit_logs` and `leave_policies` were
written where the real tables are `audit_log` and `leave_policy` (both singular),
and the migrate container exited 1 with `relation "audit_logs" does not exist`,
leaving the whole API stack stopped behind it.

This is the cheap half of the guard the enum-label test already provides.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.models.base import Base

_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# Tables an op.* call names directly.
_OP = re.compile(
    r"op\.(?:add_column|drop_column|alter_column|create_table|drop_table|"
    r"create_index|drop_index|create_foreign_key|create_unique_constraint)\(\s*\n?\s*"
    r'"([a-z_][a-z0-9_]*)"'
)
# Tables named in raw SQL inside op.execute(...).
_SQL = re.compile(r"(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)

# Index names sit in the first argument of drop_index too, so ignore anything
# that is obviously one rather than a table.
_INDEX_PREFIXES = ("ix_", "uq_", "fk_", "ck_", "pk_")


def _model_tables() -> set[str]:
    return set(Base.metadata.tables)


def test_every_migration_names_a_real_table() -> None:
    known = _model_tables()
    assert known, "no models registered — the guard would pass vacuously"

    problems: list[str] = []
    for path in sorted(_VERSIONS.glob("*.py")):
        source = path.read_text()
        named = set(_OP.findall(source)) | {m.lower() for m in _SQL.findall(source)}
        for table in sorted(named):
            if table.startswith(_INDEX_PREFIXES) or table in known:
                continue
            # A migration may legitimately touch a table that was later dropped
            # from the models; only flag the classic pluralisation slip.
            if table.rstrip("s") in known or f"{table}s" in known:
                problems.append(f"{path.name}: '{table}' — did you mean '{table.rstrip('s')}'?")

    assert not problems, "migration references a table that does not exist:\n  " + "\n  ".join(
        problems
    )


# Columns that are Postgres ENUMs. Comparing one to a bound parameter fails with
# "operator does not exist: <enum> = character varying" — Postgres will not
# implicitly cast a parameter to an enum, though a bare literal coerces fine,
# which is exactly what makes this easy to miss in review.
_ENUM_COLUMNS = ("leave_type", "tier", "status", "kind", "target", "role")

_PARAM = re.compile(r":\w+|%\(\w+\)s|\$\d+")


def test_enum_comparisons_in_raw_sql_are_cast() -> None:
    """A raw-SQL comparison of an enum column to a BOUND PARAMETER must cast it.

    `leave_type = :lt` is the shape that broke a deploy; `leave_type = 'BIRTHDAY'`
    is fine, and `leave_type = CAST(:lt AS leavetype)` is the fix.
    """
    problems: list[str] = []
    for path in sorted(_VERSIONS.glob("*.py")):
        source = path.read_text()
        for column in _ENUM_COLUMNS:
            for match in re.finditer(rf"\b{column}\s*=\s*([^\s,)]+)", source):
                value = match.group(1)
                if not _PARAM.match(value):
                    continue  # a literal, or a column — both coerce
                problems.append(
                    f"{path.name}: `{column} = {value}` compares an enum column to a "
                    f"bound parameter without a cast — use CAST({value} AS <enumtype>)"
                )
    assert not problems, "uncast enum comparison in a migration:\n  " + "\n  ".join(problems)
