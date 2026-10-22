"""Forms two people fill in: the employee's half, then their manager's

For an appraisal, where the person writes their own assessment and their
reporting manager writes the rest, on the SAME form - so both halves land in
one set of answers rather than two documents nobody lines up afterwards.

Three columns, and one rule each:

* `form_fields.stage` - who fills this question in. Null means "whoever holds
  the form", which is every field on an ordinary form. A form is two-stage if
  and only if some field says so; there is deliberately no flag on `forms` to
  drift out of step with the questions.
* `form_assignments.stage` - whose turn it is now, plus `handed_over_at`. The
  assignment stays PENDING across the handover: it is one form being filled in
  by two people, not two forms. Only `assignee_id` and `stage` move.
* `form_answers.answered_by_id` - who gave THIS answer. Without it the
  manager's half reads as the employee's words.

Nullable throughout, so every existing form and every assignment in flight
keeps behaving exactly as it does today.

Revision ID: b9d1f3a5c7e0
Revises: c7e9a1b3d5f7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d1f3a5c7e0"
down_revision: str | None = "c7e9a1b3d5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE = "formstage"
# SQLAlchemy persists the StrEnum MEMBER NAMES, so the labels are uppercase.
_STAGES = ("EMPLOYEE", "MANAGER")


def upgrade() -> None:
    sa.Enum(*_STAGES, name=_STAGE).create(op.get_bind(), checkfirst=True)
    # create_type=False: the type is made once above, and letting add_column
    # make it again raises DuplicateObject on Postgres.
    stage = postgresql.ENUM(*_STAGES, name=_STAGE, create_type=False)

    op.add_column("form_fields", sa.Column("stage", stage, nullable=True))
    op.add_column("form_assignments", sa.Column("stage", stage, nullable=True))
    op.add_column(
        "form_assignments",
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("form_answers", sa.Column("answered_by_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_form_answers_answered_by_id_employees",
        "form_answers",
        "employees",
        ["answered_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_form_answers_answered_by_id_employees", "form_answers", type_="foreignkey"
    )
    op.drop_column("form_answers", "answered_by_id")
    op.drop_column("form_assignments", "handed_over_at")
    op.drop_column("form_assignments", "stage")
    op.drop_column("form_fields", "stage")
    sa.Enum(name=_STAGE).drop(op.get_bind(), checkfirst=True)
