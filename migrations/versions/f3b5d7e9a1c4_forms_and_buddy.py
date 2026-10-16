"""Onboarding buddy on employees, and the forms tables.

- `employees.buddy_id`: the onboarding buddy, copied from Circle.
- `forms` (one row per version), `form_fields`, `form_assignments`,
  `form_responses`, `form_answers`.

Enum labels are UPPERCASE member names (see tests/test_migration_enum_labels.py).
`forms.visibility` reuses the existing `workspacevisibility` type.

Revision ID: f3b5d7e9a1c4
Revises: d7f1b3c5e9a2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3b5d7e9a1c4"
down_revision = "d7f1b3c5e9a2"
branch_labels = None
depends_on = None

_ENUMS = {
    "formstatus": ("DRAFT", "PUBLISHED", "ARCHIVED"),
    "formtrigger": ("MANUAL", "NEW_JOINER", "BUDDY"),
    "formfieldtype": ("SECTION", "RATING", "PARAGRAPH", "CHOICE"),
    "assignmentstatus": ("PENDING", "SUBMITTED", "NEEDS_BUDDY", "CANCELLED"),
}


def _enum(name: str) -> postgresql.ENUM:
    # create_type=False keeps create_table from re-emitting CREATE TYPE.
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _employee_fk(column: str, table: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["employees.id"], name=f"fk_{table}_{column}_employees", ondelete=ondelete
    )


def _add_buddy() -> None:
    op.add_column("employees", sa.Column("buddy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_employees_buddy_id_employees",
        "employees",
        "employees",
        ["buddy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_employees_buddy_id", "employees", ["buddy_id"], unique=False)


def _create_forms() -> None:
    visibility = postgresql.ENUM(
        "EVERYONE", "RESTRICTED", name="workspacevisibility", create_type=False
    )
    op.create_table(
        "forms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", _enum("formstatus"), nullable=False, server_default="DRAFT"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("intro", sa.Text(), nullable=True),
        sa.Column("rating_labels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("trigger", _enum("formtrigger"), nullable=False, server_default="MANUAL"),
        sa.Column("trigger_days", sa.Integer(), nullable=True),
        sa.Column("due_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("auto_assign_since", sa.Date(), nullable=True),
        sa.Column("visibility", visibility, nullable=False, server_default="RESTRICTED"),
        sa.Column("visible_departments", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("visible_employee_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _employee_fk("created_by", "forms", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_forms"),
        sa.UniqueConstraint("family_id", "version", name="uq_forms_family_version"),
    )
    op.create_index("ix_forms_family_id", "forms", ["family_id"], unique=False)
    op.create_index("ix_forms_status_trigger", "forms", ["status", "trigger"], unique=False)
    op.create_index(
        "uq_forms_live_trigger",
        "forms",
        ["trigger"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED' AND trigger <> 'MANUAL'"),
    )

    op.create_table(
        "form_fields",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("type", _enum("formfieldtype"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("options", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("with_comment", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name="fk_form_fields_form_id_forms", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_form_fields"),
        sa.UniqueConstraint("form_id", "position", name="uq_form_fields_position"),
    )
    op.create_index("ix_form_fields_form_id", "form_fields", ["form_id"], unique=False)


def _create_assignments() -> None:
    table = "form_assignments"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", _enum("formtrigger"), nullable=False, server_default="MANUAL"),
        sa.Column("status", _enum("assignmentstatus"), nullable=False, server_default="PENDING"),
        sa.Column("available_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hr_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name="fk_form_assignments_form_id_forms", ondelete="CASCADE"
        ),
        _employee_fk("assignee_id", table, "CASCADE"),
        _employee_fk("subject_id", table, "CASCADE"),
        _employee_fk("assigned_by", table, "SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_form_assignments"),
    )
    op.create_index("ix_form_assignments_form_id", table, ["form_id"], unique=False)
    op.create_index("ix_form_assignments_subject_id", table, ["subject_id"], unique=False)
    op.create_index(
        "ix_form_assignments_assignee_status", table, ["assignee_id", "status"], unique=False
    )
    op.create_index(
        "uq_form_assignments_trigger_subject",
        table,
        ["trigger", "subject_id"],
        unique=True,
        postgresql_where=sa.text("trigger <> 'MANUAL'"),
    )


def _create_responses() -> None:
    op.create_table(
        "form_responses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("form_id", sa.Uuid(), nullable=False),
        sa.Column("respondent_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["form_assignments.id"],
            name="fk_form_responses_assignment_id_form_assignments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["form_id"], ["forms.id"], name="fk_form_responses_form_id_forms", ondelete="CASCADE"
        ),
        _employee_fk("respondent_id", "form_responses", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_form_responses"),
        sa.UniqueConstraint("assignment_id", name="uq_form_responses_assignment_id"),
    )
    op.create_index("ix_form_responses_form_id", "form_responses", ["form_id"], unique=False)

    op.create_table(
        "form_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("response_id", sa.Uuid(), nullable=False),
        sa.Column("field_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("choice", sa.String(length=200), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["form_responses.id"],
            name="fk_form_answers_response_id_form_responses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["form_fields.id"],
            name="fk_form_answers_field_id_form_fields",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_form_answers"),
        sa.UniqueConstraint("response_id", "field_id", name="uq_form_answers_response_field"),
    )
    op.create_index("ix_form_answers_response_id", "form_answers", ["response_id"], unique=False)
    op.create_index("ix_form_answers_field_id", "form_answers", ["field_id"], unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name in _ENUMS:
        _enum(name).create(bind, checkfirst=True)
    _add_buddy()
    _create_forms()
    _create_assignments()
    _create_responses()


def downgrade() -> None:
    for table in ("form_answers", "form_responses", "form_assignments", "form_fields", "forms"):
        op.drop_table(table)
    op.drop_index("ix_employees_buddy_id", table_name="employees")
    op.drop_constraint("fk_employees_buddy_id_employees", "employees", type_="foreignkey")
    op.drop_column("employees", "buddy_id")
    bind = op.get_bind()
    for name in _ENUMS:
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)
