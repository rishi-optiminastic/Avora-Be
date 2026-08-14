"""enum label casing: TASK_COMMENT + tenurestatus bands

SQLAlchemy persists a Python Enum column by its MEMBER NAME, not its value — so
`NotificationKind.TASK_COMMENT` is written as `TASK_COMMENT`, never `task_comment`.
Every enum label in this database is therefore uppercase (see the reimbursements
migration, which correctly adds `REIMBURSEMENT_SUBMITTED`).

Two earlier migrations broke that rule and shipped lowercase labels, so the values
existed but nothing could ever be written with them:

  e7a9c1b3d5f8  added 'task_comment'  -> posting a task comment still 500s
  b2d4f6a8c0e3  created `tenurestatus` as ('probation','confirmed','tenured')
                -> the leave balance 500s on the tier seed, taking every leave
                   page down with it

Postgres cannot remove an enum label, so this adds the correctly-cased ones
alongside. The lowercase strays are unreachable from the ORM and harmless.

Revision ID: d4f6b8c0e2a5
Revises: c3e5a7b9d1f4
Create Date: 2026-08-14 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4f6b8c0e2a5"
down_revision: str | Sequence[str] | None = "c3e5a7b9d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (enum type, member names as the ORM will write them)
_MISSING_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("notificationkind", ("TASK_COMMENT",)),
    ("tenurestatus", ("PROBATION", "CONFIRMED", "TENURED")),
)


def upgrade() -> None:
    for enum_name, labels in _MISSING_LABELS:
        for label in labels:
            op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # Postgres cannot drop an enum label; leaving them is harmless and reversible
    # only by recreating the type, which would require rewriting every column
    # that uses it. Deliberately a no-op.
    pass
