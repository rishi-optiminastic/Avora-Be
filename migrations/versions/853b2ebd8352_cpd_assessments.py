"""CPD assessments - tests HR writes, assigns, and that mark themselves.

Four tables rather than an extension of `forms`. The forms engine scores by
weight times a 1-5 rating - a subjective judgement about a person - and a test
scores against an answer key. The deciding detail is that key: a `form_fields`
row has nowhere to put one, and adding a `correct` column there would make
every answer readable by everyone who can already read a form.

What is reused instead is the audience: "everyone / these departments / these
people" is answered once in `AudienceResolver`, shared with events and form
sends, and a test assignment asks exactly that question.

Snapshots, for the reason every other scored thing in this schema snapshots:
`assessment_attempts` stores `score`, `max_score`, `percentage` and `passed`
at submission, and `assessment_answers` stores `is_correct`. Editing a
question or raising the pass mark afterwards changes what happens NEXT, never
what somebody already scored.

Revision ID: 853b2ebd8352
Revises: 0958c8750ec0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "853b2ebd8352"
down_revision = "0958c8750ec0"
branch_labels = None
depends_on = None

# SQLAlchemy persists StrEnum MEMBER NAMES, so every label here is uppercase.
_STATUS = sa.Enum("DRAFT", "PUBLISHED", "ARCHIVED", name="assessmentstatus")
_KIND = sa.Enum("SINGLE", "MULTI", name="questionkind")
_ATTEMPT = sa.Enum("ASSIGNED", "SUBMITTED", name="attemptstatus")


def _enum(name: str) -> postgresql.ENUM:
    # Reference by name only: the types are created once, up front. A copied
    # sa.Enum keeps create_type=True and aborts on "type already exists".
    return postgresql.ENUM(name=name, create_type=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_STATUS, _KIND, _ATTEMPT):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "assessments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("status", _enum("assessmentstatus"), nullable=False, server_default="DRAFT"),
        sa.Column("pass_mark", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "created_by",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_assessments_status_created", "assessments", ["status", "created_at"])

    op.create_table(
        "assessment_questions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "assessment_id",
            sa.UUID(),
            sa.ForeignKey("assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.String(length=1000), nullable=False),
        sa.Column("kind", _enum("questionkind"), nullable=False, server_default="SINGLE"),
        sa.Column("options", sa.JSON(), nullable=False),
        # The answer key. Never serialised to a taker.
        sa.Column("correct", sa.JSON(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
    )
    op.create_index(
        "ix_assessment_questions_assessment_id", "assessment_questions", ["assessment_id"]
    )
    op.create_unique_constraint(
        "uq_assessment_question_position", "assessment_questions", ["assessment_id", "position"]
    )

    op.create_table(
        "assessment_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "assessment_id",
            sa.UUID(),
            sa.ForeignKey("assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", _enum("attemptstatus"), nullable=False, server_default="ASSIGNED"),
        sa.Column(
            "assigned_by",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("due_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("percentage", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        # The pass mark it was judged against, beside the verdict.
        sa.Column("pass_mark", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_assessment_attempts_assessment_id", "assessment_attempts", ["assessment_id"]
    )
    op.create_index("ix_assessment_attempts_employee_id", "assessment_attempts", ["employee_id"])
    op.create_index("ix_attempts_employee_status", "assessment_attempts", ["employee_id", "status"])
    # One sitting per person per test: re-assigning tops up rather than
    # handing anyone a second copy.
    op.create_unique_constraint(
        "uq_attempt_assessment_employee", "assessment_attempts", ["assessment_id", "employee_id"]
    )

    op.create_table(
        "assessment_answers",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.UUID(),
            sa.ForeignKey("assessment_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.UUID(),
            sa.ForeignKey("assessment_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chosen", sa.JSON(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("points_awarded", sa.Float(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index("ix_assessment_answers_attempt_id", "assessment_answers", ["attempt_id"])
    op.create_index("ix_assessment_answers_question_id", "assessment_answers", ["question_id"])
    op.create_unique_constraint(
        "uq_answer_attempt_question", "assessment_answers", ["attempt_id", "question_id"]
    )


def downgrade() -> None:
    op.drop_table("assessment_answers")
    op.drop_table("assessment_attempts")
    op.drop_table("assessment_questions")
    op.drop_index("ix_assessments_status_created", table_name="assessments")
    op.drop_table("assessments")
    bind = op.get_bind()
    for enum in (_ATTEMPT, _KIND, _STATUS):
        enum.drop(bind, checkfirst=True)
