"""Playbooks: a checklist written once, stamped out per person as real tasks.

Measured on the live database: 74 onboarding-shaped tasks typed by hand by
one HR lead across 41 distinct steps, 32 more by another - roughly 106 tasks
of retyping for checklists that never changed. A playbook replaces that with
one template plus a name.

Four changes:

* `playbooks` - the template. `auto_assign_since` is the backfill guard: only
  subjects whose anchor date is on or after it are picked up automatically,
  so publishing a new-joiner playbook does not fire for all 29 people already
  on the roster.
* `playbook_steps` - one task template each, with a `{name}`-style title, an
  assignee RULE rather than an id, and a day offset from the run's anchor.
* `playbook_runs` - one instantiation. Its partial unique index is the
  idempotency guard, keyed by (trigger, subject, PERIOD). The period is what
  lets one table serve both shapes of automation: "" means once ever - a
  person is onboarded once - and "YYYY-MM" means once a month, so November's
  close and December's are different runs and neither can happen twice.
  Hard-coding once-ever would have made a monthly playbook refuse to run
  again after its first month.

  Unlike `uq_form_assignments_trigger_subject` it also EXCLUDES cancelled
  rows, so a run can be undone and done again. That omission is precisely why
  a probation form could not be cancelled and re-sent.

* `playbooks.owner_id` - who a non-lifecycle playbook is FOR. A new-joiner
  playbook is about whoever joined, so the event supplies the subject; a
  monthly compliance checklist belongs to a desk, so it has to be named.
* `tasks.playbook_run_id` - provenance, so "where did this task come from?"
  has an answer. SET NULL on delete: cancelling a run removes its untouched
  tasks deliberately, but a task somebody has since worked on must survive
  losing its provenance rather than vanish with it.

Enum values are created explicitly rather than inline, because a CREATE TYPE
inside the same transaction as a table that uses it fails on Postgres under
the schema-per-workspace setup.

Revision ID: c2d4f6a8b0e3
Revises: d3e5f7a9c1b2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c2d4f6a8b0e3"
down_revision = "d3e5f7a9c1b2"
branch_labels = None
depends_on = None

# SQLAlchemy persists StrEnum MEMBER NAMES, so every label here is uppercase.
_TRIGGER = sa.Enum(
    "MANUAL",
    "NEW_JOINER",
    "OFFBOARDING",
    "MONTH_START",
    "MONTH_END",
    name="playbooktrigger",
)
_STATUS = sa.Enum("DRAFT", "PUBLISHED", "ARCHIVED", name="playbookstatus")
_RULE = sa.Enum("SUBJECT", "SUBJECT_MANAGER", "SUBJECT_BUDDY", "FIXED", name="assigneerule")
_RUN_STATUS = sa.Enum("COMPLETED", "CANCELLED", name="playbookrunstatus")


def _enum(name: str) -> postgresql.ENUM:
    # Reference by name only: the types are created once, up front. A copied
    # sa.Enum keeps create_type=True and aborts on "type already exists" when
    # create_table emits its own CREATE TYPE as well.
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_TRIGGER, _STATUS, _RULE, _RUN_STATUS):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "playbooks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", _enum("playbookstatus"), nullable=False, server_default="DRAFT"),
        sa.Column("trigger", _enum("playbooktrigger"), nullable=False, server_default="MANUAL"),
        sa.Column("trigger_days", sa.Integer(), nullable=True),
        sa.Column("auto_assign_since", sa.Date(), nullable=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.Date(), nullable=True),
    )
    op.create_index("ix_playbooks_status_trigger", "playbooks", ["status", "trigger"])
    op.create_index(
        "uq_playbooks_live_trigger",
        "playbooks",
        ["trigger"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'PUBLISHED' AND trigger IN ('NEW_JOINER', 'OFFBOARDING')"
        ),
        sqlite_where=sa.text("status = 'PUBLISHED' AND trigger IN ('NEW_JOINER', 'OFFBOARDING')"),
    )

    op.create_table(
        "playbook_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "playbook_id",
            sa.Uuid(),
            sa.ForeignKey("playbooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title_template", sa.String(256), nullable=False),
        sa.Column("description_template", sa.String(2000), nullable=True),
        sa.Column("assignee_rule", _enum("assigneerule"), nullable=False, server_default="SUBJECT"),
        sa.Column(
            "fixed_assignee_id",
            sa.Uuid(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("offset_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priority", _enum("taskpriority"), nullable=False, server_default="MEDIUM"),
        sa.Column("expected_output", sa.String(2000), nullable=True),
    )
    op.create_index("ix_playbook_steps_playbook_id", "playbook_steps", ["playbook_id"])
    op.create_index(
        "ix_playbook_steps_playbook_position", "playbook_steps", ["playbook_id", "position"]
    )

    op.create_table(
        "playbook_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "playbook_id",
            sa.Uuid(),
            sa.ForeignKey("playbooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", _enum("playbooktrigger"), nullable=False, server_default="MANUAL"),
        sa.Column("status", _enum("playbookrunstatus"), nullable=False, server_default="COMPLETED"),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("period_key", sa.String(16), nullable=False, server_default=""),
        sa.Column("tasks_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ran_by", sa.Uuid(), sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index("ix_playbook_runs_playbook_id", "playbook_runs", ["playbook_id"])
    op.create_index("ix_playbook_runs_subject", "playbook_runs", ["subject_id"])
    op.create_index(
        "uq_playbook_runs_trigger_subject",
        "playbook_runs",
        ["trigger", "subject_id", "period_key"],
        unique=True,
        postgresql_where=sa.text("trigger <> 'MANUAL' AND status <> 'CANCELLED'"),
        sqlite_where=sa.text("trigger <> 'MANUAL' AND status <> 'CANCELLED'"),
    )

    op.add_column("tasks", sa.Column("playbook_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_playbook_run",
        "tasks",
        "playbook_runs",
        ["playbook_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tasks_playbook_run_id", "tasks", ["playbook_run_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_playbook_run_id", table_name="tasks")
    op.drop_constraint("fk_tasks_playbook_run", "tasks", type_="foreignkey")
    op.drop_column("tasks", "playbook_run_id")

    op.drop_index("uq_playbook_runs_trigger_subject", table_name="playbook_runs")
    op.drop_index("ix_playbook_runs_subject", table_name="playbook_runs")
    op.drop_index("ix_playbook_runs_playbook_id", table_name="playbook_runs")
    op.drop_table("playbook_runs")

    op.drop_index("ix_playbook_steps_playbook_position", table_name="playbook_steps")
    op.drop_index("ix_playbook_steps_playbook_id", table_name="playbook_steps")
    op.drop_table("playbook_steps")

    op.drop_index("uq_playbooks_live_trigger", table_name="playbooks")
    op.drop_index("ix_playbooks_status_trigger", table_name="playbooks")
    op.drop_table("playbooks")

    bind = op.get_bind()
    for enum in (_RUN_STATUS, _RULE, _STATUS, _TRIGGER):
        enum.drop(bind, checkfirst=True)
