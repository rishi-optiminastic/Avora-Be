"""Events, and the EVENT notification kind

An event is something HR or an admin wants people at; the form attached to it
is how answers are collected (RSVP, feedback, sign-up). There is deliberately
no `event_responses` table - `form_responses` already stores answers, and a
second mechanism for the same thing is a second place to look.

Audience is stored as a CLAIM (kind + departments or ids), not a snapshot of
people. "Everyone" written today has to mean the roster next month, and a
frozen list would silently exclude every joiner since.

Revision ID: a3c5e7b9d1f2
Revises: d6f8a0c2e4b7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3c5e7b9d1f2"
down_revision: str | None = "d6f8a0c2e4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = "eventstatus"
_AUDIENCE = "audiencekind"
# SQLAlchemy persists the StrEnum MEMBER NAMES, so the labels are uppercase.
_STATUSES = ("DRAFT", "PUBLISHED", "CANCELLED")
_AUDIENCES = ("EVERYONE", "DEPARTMENTS", "PEOPLE")


def upgrade() -> None:
    sa.Enum(*_STATUSES, name=_STATUS).create(op.get_bind(), checkfirst=True)
    sa.Enum(*_AUDIENCES, name=_AUDIENCE).create(op.get_bind(), checkfirst=True)
    # create_type=False: the types are made once above, and letting
    # create_table make them again raises DuplicateObject on Postgres.
    status = postgresql.ENUM(*_STATUSES, name=_STATUS, create_type=False)
    audience = postgresql.ENUM(*_AUDIENCES, name=_AUDIENCE, create_type=False)

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", status, nullable=False, server_default="DRAFT"),
        sa.Column("audience_kind", audience, nullable=False, server_default="EVERYONE"),
        sa.Column("audience_departments", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("audience_employee_ids", sa.JSON(), nullable=False, server_default="[]"),
        # SET NULL, not CASCADE: deleting a draft form must not take the event
        # with it, and an event whose form is gone is still a real event.
        sa.Column("form_id", sa.Uuid(), nullable=True),
        sa.Column("form_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["form_id"], ["forms.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_events_starts_at", "events", ["starts_at"])
    op.create_index("ix_events_status", "events", ["status"])
    op.create_index("ix_events_form_id", "events", ["form_id"])
    op.create_index("ix_events_status_starts_at", "events", ["status", "starts_at"])

    op.execute("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'EVENT'")


def downgrade() -> None:
    op.drop_index("ix_events_status_starts_at", table_name="events")
    op.drop_index("ix_events_form_id", table_name="events")
    op.drop_index("ix_events_status", table_name="events")
    op.drop_index("ix_events_starts_at", table_name="events")
    op.drop_table("events")
    sa.Enum(name=_AUDIENCE).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=_STATUS).drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop a single enum value without rebuilding the type; an
    # unused 'EVENT' label on notificationkind is harmless.
