"""Access - brands, and which brand each tech asset belongs to.

The company runs several brands (The 3D Printing Store, SCRN Studio, Carboncut
...) whose accounts, domains and subscriptions were mixed together in the
Tech Tracker sheet. A brand row plus `tech_assets.brand_id` lets the catalog,
canvas and finance spend be read per brand. SET NULL: removing a brand never
removes the assets that carried it.

Revision ID: a3c5e7f9b1d4
Revises: f2b4d6e8a0c3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c5e7f9b1d4"
down_revision = "f2b4d6e8a0c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_brands_name"),
    )
    op.add_column(
        "tech_assets",
        sa.Column(
            "brand_id",
            sa.UUID(),
            sa.ForeignKey("brands.id", ondelete="SET NULL", name="fk_tech_assets_brand_id_brands"),
            nullable=True,
        ),
    )
    op.create_index("ix_tech_assets_brand_id", "tech_assets", ["brand_id"])


def downgrade() -> None:
    op.drop_index("ix_tech_assets_brand_id", table_name="tech_assets")
    op.drop_column("tech_assets", "brand_id")
    op.drop_table("brands")
