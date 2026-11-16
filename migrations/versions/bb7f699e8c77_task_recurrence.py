"""Make a recurring task actually recur.

`tasks.cadence` has existed since the tasks module shipped and nothing ever
read it. On the live database 509 tasks carry a recurring cadence - 384 DAILY,
100 MONTHLY, 25 WEEKLY - and not one has ever produced a second instance.
People have been ticking "daily" and re-creating the work by hand every
morning. This adds what the regeneration needs.

* `series_id` links every instance of one recurring task. NOT `parent_task_id`,
  which already means sub-task; overloading it would make "show me the
  sub-tasks" start returning last week's copy. SET NULL on delete, so removing
  the first instance never takes a year of completed work with it.
* `recurs_until` ends a series without deleting its history.
* `recurrence_active` pauses one - an assignee on leave for a month should
  stop generating, not be rebuilt from scratch afterwards.

NO BACKFILL. The existing 509 tasks keep a null `series_id` and become the
first instance of their own series the first time the worker generates from
them. Stamping a series id onto a year of history would claim those rows were
produced by a scheduler that did not exist.

The partial index is what makes the sweep cheap: it only ever asks for live,
recurring, active tasks, which is a small slice of a large table.

Revision ID: bb7f699e8c77
Revises: 853b2ebd8352
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "bb7f699e8c77"
down_revision = "853b2ebd8352"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("series_id", sa.UUID(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
    )
    op.add_column("tasks", sa.Column("recurs_until", sa.Date(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("recurrence_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_tasks_series_id", "tasks", ["series_id"])
    # NO index for the sweep itself. One was written here on the assumption
    # that scanning 509 recurring rows out of 1,830 needed help; EXPLAIN on a
    # production-shaped table says otherwise - the planner picks the existing
    # `ix_tasks_cadence` and the plan is identical with a dedicated index, a
    # corrected one, or none at all. An index nothing reads still costs a
    # write on every task.
    #
    # It would also have been unusable as first written: it required
    # `status <> 'DONE'` while the sweep deliberately does NOT filter status
    # (completing Monday's task is exactly when Tuesday's should appear), so
    # Postgres could never prove the predicate held.


def downgrade() -> None:
    op.drop_index("ix_tasks_series_id", table_name="tasks")
    op.drop_column("tasks", "recurrence_active")
    op.drop_column("tasks", "recurs_until")
    op.drop_column("tasks", "series_id")
