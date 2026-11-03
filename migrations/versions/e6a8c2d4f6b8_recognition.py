"""recognitions + recognition_comments — the employee recognition programme

A manager awards one of ten badges to someone who earned it; the whole org sees
it and can add to the thread. Badges themselves are NOT rows: they are a
published vocabulary with agreed meanings, so they live in
app/core/recognition_badges.py where a typo cannot invent an eleventh badge.

Revision ID: e6a8c2d4f6b8
Revises: d5f7b9c1e3a4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6a8c2d4f6b8"
down_revision = "d5f7b9c1e3a4"
branch_labels = None
depends_on = None

_BADGE = sa.Enum(
    "CONSISTENCY_STAR",
    "LEARNING_WIN",
    "CULTURE_CHAMPION",
    "MOST_IMPROVED",
    "CLIENT_APPRECIATION",
    "WIN_OF_THE_WEEK",
    "ABOVE_AND_BEYOND",
    "TEAM_PLAYER",
    "IDEA_OF_THE_WEEK",
    "TARGET_CRUSHER",
    name="recognitionbadge",
)


def upgrade() -> None:
    # Created up front so the column definition does not emit its own CREATE
    # TYPE: with several workspace schemas on one database, that races itself
    # inside a single transaction.
    _BADGE.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "recognitions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: the recognition outlives the manager who gave
        # it, and offboarding them must not erase somebody's record.
        sa.Column(
            "awarded_by_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("badge", _BADGE.copy(), nullable=False),  # type: ignore[attr-defined]
        sa.Column("note", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recognitions_employee_id", "recognitions", ["employee_id"])
    op.create_index("ix_recognitions_awarded_by_id", "recognitions", ["awarded_by_id"])
    op.create_index("ix_recognitions_badge", "recognitions", ["badge"])
    op.create_index(
        "ix_recognitions_employee_created", "recognitions", ["employee_id", "created_at"]
    )

    op.create_table(
        "recognition_comments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "recognition_id",
            sa.UUID(),
            sa.ForeignKey("recognitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_recognition_comments_recognition_id", "recognition_comments", ["recognition_id"]
    )
    op.create_index("ix_recognition_comments_author_id", "recognition_comments", ["author_id"])
    op.create_index(
        "ix_recognition_comments_recognition_created",
        "recognition_comments",
        ["recognition_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("recognition_comments")
    op.drop_table("recognitions")
    _BADGE.drop(op.get_bind(), checkfirst=True)
