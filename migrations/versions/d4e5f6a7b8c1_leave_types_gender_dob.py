"""leave types expansion + employee date_of_birth / gender + per-type quotas

Adds:
- employees.date_of_birth (nullable) + employees.gender (new `gender` enum, nullable)
- leave_policy per-type annual quotas (annual/bereavement/birthday/maternity/
  paternity/marriage), NOT NULL with sensible defaults for the singleton row
- leave_allocations per-type overrides (nullable — null = use policy default)
- new `leavetype` enum values: ANNUAL, BEREAVEMENT, BIRTHDAY, MATERNITY,
  PATERNITY, MARRIAGE

Revision ID: d4e5f6a7b8c1
Revises: a9b8c7d6e5f4
Create Date: 2026-07-10 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c1"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# New paid leave types + their leave_policy default annual quota (working days).
_POLICY_QUOTAS: tuple[tuple[str, int], ...] = (
    ("annual_days", 15),
    ("bereavement_days", 5),
    ("birthday_days", 1),
    ("maternity_days", 90),
    ("paternity_days", 15),
    ("marriage_days", 5),
)

_NEW_LEAVE_TYPES = ("ANNUAL", "BEREAVEMENT", "BIRTHDAY", "MATERNITY", "PATERNITY", "MARRIAGE")


def upgrade() -> None:
    # --- employee: date of birth + gender ---------------------------------- #
    gender = sa.Enum("MALE", "FEMALE", "OTHER", name="gender")
    gender.create(op.get_bind(), checkfirst=True)
    op.add_column("employees", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column(
        "employees",
        sa.Column(
            "gender",
            postgresql.ENUM("MALE", "FEMALE", "OTHER", name="gender", create_type=False),
            nullable=True,
        ),
    )

    # --- leave_policy: per-type annual quotas (NOT NULL, defaulted) --------- #
    for column, default in _POLICY_QUOTAS:
        op.add_column(
            "leave_policy",
            sa.Column(column, sa.Integer(), nullable=False, server_default=str(default)),
        )

    # --- leave_allocations: per-type overrides (nullable) ------------------ #
    for column, _default in _POLICY_QUOTAS:
        op.add_column("leave_allocations", sa.Column(column, sa.Integer(), nullable=True))

    # --- leavetype: new enum values ---------------------------------------- #
    # PostgreSQL 12+ allows ADD VALUE inside a transaction (the value is only
    # used after commit, which is the case here). Idempotent.
    for value in _NEW_LEAVE_TYPES:
        op.execute(f"ALTER TYPE leavetype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    for column, _default in _POLICY_QUOTAS:
        op.drop_column("leave_allocations", column)
        op.drop_column("leave_policy", column)
    op.drop_column("employees", "gender")
    op.drop_column("employees", "date_of_birth")
    sa.Enum(name="gender").drop(op.get_bind(), checkfirst=True)
    # PostgreSQL cannot remove values from an enum type; the added `leavetype`
    # values remain (harmless — no rows are forced to use them).
