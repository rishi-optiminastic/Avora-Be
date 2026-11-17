"""Mark a task the screen-activity detector raised, so the UI can say so.

A task can now arrive without anybody typing it in three ways, and each one
needs a different explanation when somebody asks "where did this come from?":

* `series_id`       - recurrence regenerated it from the last instance
* `playbook_run_id` - a playbook run stamped it out
* `auto_detected`   - the detector saw sustained work on a project with no
                      task behind it

A boolean rather than a fourth foreign key, because unlike the other two this
one has nothing of its own to point AT: the referent is the work entity, and
`tasks.project_id` already carries it. `auto_detected` plus `project_id` is
the whole provenance.

Deliberately NOT folded into one `created_by` enum. That would be the tidier
schema, but it would rewrite how recurrence and playbooks record themselves -
both already live and tested - for a cosmetic gain.

Revision ID: e4f6a8b0c2d5
Revises: c2d4f6a8b0e3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4f6a8b0c2d5"
down_revision = "c2d4f6a8b0e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("auto_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Partial: the detector asks "which of this person's entities already have
    # a task", not "which tasks are detected", so the index only has to cover
    # the handful that are. Every existing row is false.
    op.create_index(
        "ix_tasks_auto_detected",
        "tasks",
        ["assignee_id", "project_id"],
        postgresql_where=sa.text("auto_detected"),
        sqlite_where=sa.text("auto_detected"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_auto_detected", table_name="tasks")
    op.drop_column("tasks", "auto_detected")
