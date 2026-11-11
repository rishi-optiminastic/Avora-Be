"""Rewards - recognition points, peer awards, and what points buy.

Recognition existed but was used ONCE in its lifetime, and the reason was
structural rather than cosmetic: only HR, admins and a recipient's own
reporting manager could award, which on a 29-person org is 8 possible givers
and 21 people with no way to recognise anybody. This opens awarding to
everyone and gives the points somewhere to go.

Three parts:

1. `recognitions.source` - MANAGER (the recipient's line, HR or an admin) or
   PEER (anyone else). Stored, not re-derived: `manager_id` moves, and a
   reorg must not retroactively downgrade a manager's badge to a peer one.

2. `recognitions.points` - what the award was worth, SNAPSHOT at award time.
   Never recomputed from the badge, because people spend these on real things
   and repricing a badge would silently move balances that were already spent.

3. `reward_items` / `reward_redemptions` - the catalogue and the spending.
   A balance is always `earned - spent` over these rows and is never stored,
   so there is no counter to drift away from the rows that explain it.

BACKFILL. Every existing recognition becomes MANAGER, and that is exact
rather than a guess: under the permission rule this migration replaces, a
non-manager could not have awarded one. Points are backfilled from the badge
values as published at this revision, written out here rather than imported
from `app.core.recognition_badges` - a migration that reads today's code
would rewrite history the next time somebody edits a price.

Revision ID: 0958c8750ec0
Revises: a3c5e7f9b1d4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0958c8750ec0"
down_revision = "a3c5e7f9b1d4"
branch_labels = None
depends_on = None

# SQLAlchemy persists StrEnum MEMBER NAMES, so every label here is uppercase.
_SOURCE = sa.Enum("MANAGER", "PEER", name="recognitionsource")
_REDEMPTION = sa.Enum(
    "REQUESTED", "APPROVED", "FULFILLED", "DECLINED", "CANCELLED", name="redemptionstatus"
)

# The published values at this revision. Frozen here on purpose: see BACKFILL.
_POINTS_AT_THIS_REVISION = {
    "CONSISTENCY_STAR": 50,
    "LEARNING_WIN": 40,
    "CULTURE_CHAMPION": 40,
    "MOST_IMPROVED": 50,
    "CLIENT_APPRECIATION": 60,
    "WIN_OF_THE_WEEK": 20,
    "ABOVE_AND_BEYOND": 30,
    "TEAM_PLAYER": 20,
    "IDEA_OF_THE_WEEK": 25,
    "TARGET_CRUSHER": 30,
}


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
    for enum in (_SOURCE, _REDEMPTION):
        enum.create(bind, checkfirst=True)

    # --- 1 + 2. points and provenance on the award itself ------------------ #
    # Added with a server_default so the existing rows are valid the moment
    # the column exists, then dropped: the application always supplies both,
    # and a lingering default is how a bug becomes a silent zero.
    op.add_column(
        "recognitions",
        sa.Column("source", _enum("recognitionsource"), nullable=False, server_default="MANAGER"),
    )
    op.add_column(
        "recognitions", sa.Column("points", sa.Integer(), nullable=False, server_default="0")
    )
    for badge, points in _POINTS_AT_THIS_REVISION.items():
        bind.execute(
            sa.text(
                "UPDATE recognitions SET points = :points"
                " WHERE badge = CAST(:badge AS recognitionbadge)"
            ),
            {"points": points, "badge": badge},
        )
    op.alter_column("recognitions", "source", server_default=None)
    op.alter_column("recognitions", "points", server_default=None)

    # --- 3. the catalogue -------------------------------------------------- #
    op.create_table(
        "reward_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("points_cost", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column(
            "created_by",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_timestamps(),
    )
    op.create_index("ix_reward_items_active_cost", "reward_items", ["is_active", "points_cost"])

    op.create_table(
        "reward_redemptions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, with the title and cost snapshotted beside it: retiring an
        # item must never erase what somebody already redeemed.
        sa.Column(
            "item_id",
            sa.UUID(),
            sa.ForeignKey("reward_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("item_title", sa.String(length=160), nullable=False),
        sa.Column("points_spent", sa.Integer(), nullable=False),
        sa.Column("status", _enum("redemptionstatus"), nullable=False, server_default="REQUESTED"),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column(
            "decided_by",
            sa.UUID(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_reward_redemptions_employee_id", "reward_redemptions", ["employee_id"])
    op.create_index("ix_reward_redemptions_item_id", "reward_redemptions", ["item_id"])
    op.create_index(
        "ix_reward_redemptions_employee_status", "reward_redemptions", ["employee_id", "status"]
    )
    op.create_index(
        "ix_reward_redemptions_status_created", "reward_redemptions", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("reward_redemptions")
    op.drop_index("ix_reward_items_active_cost", table_name="reward_items")
    op.drop_table("reward_items")
    op.drop_column("recognitions", "points")
    op.drop_column("recognitions", "source")
    bind = op.get_bind()
    for enum in (_REDEMPTION, _SOURCE):
        enum.drop(bind, checkfirst=True)
