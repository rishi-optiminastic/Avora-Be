"""Access - tech assets, who holds them, and requests for them.

Replaces the Tech Tracker spreadsheet, where who held a login lived in a
free-text "Shared With" column and nobody could answer "who can get into
Vercel" without reading every row. See app/models/access.py.

The asset's password or key is stored Fernet-encrypted in
`tech_assets.secret_ciphertext`; nothing here holds plaintext.

Revision ID: e1a3c5f7b9d2
Revises: d8f0a2c4e6b9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1a3c5f7b9d2"
down_revision = "d8f0a2c4e6b9"
branch_labels = None
depends_on = None

_KIND = sa.Enum(
    "ACCOUNT", "TOOL", "SUBSCRIPTION", "DOMAIN", "HOSTING", "API_KEY", "PHONE", name="techassetkind"
)
_LEVEL = sa.Enum("OWNER", "ADMIN", "EDITOR", "VIEWER", name="accesslevel")
_PRIORITY = sa.Enum("CRITICAL", "HIGH", "MEDIUM", "LOW", name="assetpriority")
_STATUS = sa.Enum("ACTIVE", "TRIAL", "PAUSED", "CANCELLED", "EXPIRED", name="assetstatus")
_REQUEST = sa.Enum("PENDING", "APPROVED", "REJECTED", "CANCELLED", name="accessrequeststatus")


def _enum(name: str) -> postgresql.ENUM:
    # Reference by name only: the types are created once, up front. A copied
    # sa.Enum keeps create_type=True and aborts on "type already exists".
    return postgresql.ENUM(name=name, create_type=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _employee_fk(name: str, *, nullable: bool = True, cascade: bool = False) -> sa.Column[object]:
    ondelete = "CASCADE" if cascade else "SET NULL"
    return sa.Column(
        name, sa.UUID(), sa.ForeignKey("employees.id", ondelete=ondelete), nullable=nullable
    )


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_KIND, _LEVEL, _PRIORITY, _STATUS, _REQUEST):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "tech_assets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("kind", _enum("techassetkind"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column(
            "parent_id",
            sa.UUID(),
            sa.ForeignKey("tech_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _employee_fk("owner_id"),
        sa.Column("login_url", sa.String(length=500), nullable=True),
        sa.Column("login_id", sa.String(length=320), nullable=True),
        sa.Column("priority", _enum("assetpriority"), nullable=False, server_default="MEDIUM"),
        sa.Column("status", _enum("assetstatus"), nullable=False, server_default="ACTIVE"),
        sa.Column("two_factor_method", sa.String(length=64), nullable=True),
        sa.Column("cost_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("billing_cycle", sa.String(length=32), nullable=True),
        sa.Column("renews_on", sa.Date(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        _employee_fk("secret_rotated_by"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_tech_assets_kind", "tech_assets", ["kind"])
    op.create_index("ix_tech_assets_kind_name", "tech_assets", ["kind", "name"])
    op.create_index("ix_tech_assets_parent_id", "tech_assets", ["parent_id"])
    op.create_index("ix_tech_assets_owner_id", "tech_assets", ["owner_id"])
    op.create_index("ix_tech_assets_renews_on", "tech_assets", ["renews_on"])

    op.create_table(
        "access_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(),
            sa.ForeignKey("tech_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _employee_fk("employee_id", nullable=False, cascade=True),
        sa.Column("level", _enum("accesslevel"), nullable=False, server_default="VIEWER"),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", _enum("accessrequeststatus"), nullable=False, server_default="PENDING"),
        _employee_fk("reviewed_by_id"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_access_requests_asset_id", "access_requests", ["asset_id"])
    op.create_index("ix_access_requests_employee_id", "access_requests", ["employee_id"])
    op.create_index("ix_access_requests_status", "access_requests", ["status"])
    op.create_index(
        "ix_access_requests_employee_status", "access_requests", ["employee_id", "status"]
    )

    op.create_table(
        "access_grants",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(),
            sa.ForeignKey("tech_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _employee_fk("employee_id", nullable=False, cascade=True),
        sa.Column("level", _enum("accesslevel"), nullable=False, server_default="VIEWER"),
        _employee_fk("granted_by"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request_id",
            sa.UUID(),
            sa.ForeignKey("access_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _employee_fk("revoked_by"),
        *_timestamps(),
    )
    op.create_index("ix_access_grants_asset_id", "access_grants", ["asset_id"])
    op.create_index("ix_access_grants_employee_id", "access_grants", ["employee_id"])
    op.create_index("ix_access_grants_revoked_at", "access_grants", ["revoked_at"])
    op.create_index(
        "uq_access_grants_live",
        "access_grants",
        ["asset_id", "employee_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("access_grants")
    op.drop_table("access_requests")
    op.drop_table("tech_assets")
    bind = op.get_bind()
    for enum in (_REQUEST, _STATUS, _PRIORITY, _LEVEL, _KIND):
        enum.drop(bind, checkfirst=True)
