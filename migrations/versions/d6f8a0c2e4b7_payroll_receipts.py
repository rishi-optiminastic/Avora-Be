"""Payroll receipts - proof a month's payroll was actually paid

The payroll run records what SHOULD have gone out. This records that it did:
the bank's transfer advice, a UTR confirmation, the statement line. Filed by
whoever ran payroll, read by the same people.

Deliberately separate from `payout_uploads`, which is the bank FILE parsed into
rows and reconciled line by line. That answers "did the right amounts reach the
right people"; this answers "show me the run happened", which is what an audit,
a dispute or a departing employee asks for.

Several rows per month is expected and allowed - salaries and reimbursements
often go out as separate transfers - so there is no unique constraint on the
month. Duplicate FILES are caught in the service on `file_sha256`.

Revision ID: d6f8a0c2e4b7
Revises: c5e7a9b1d3f6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6f8a0c2e4b7"
down_revision: str | None = "c5e7a9b1d3f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = "payrollreceiptkind"
# SQLAlchemy persists the StrEnum MEMBER NAMES, so the labels are uppercase.
_KINDS = ("BANK_ADVICE", "UTR_CONFIRMATION", "BANK_STATEMENT", "OTHER")


def upgrade() -> None:
    sa.Enum(*_KINDS, name=_KIND).create(op.get_bind(), checkfirst=True)
    # Referenced with create_type=False: the type is made once above, and
    # letting create_table make it again raises DuplicateObject on Postgres.
    kind = postgresql.ENUM(*_KINDS, name=_KIND, create_type=False)

    op.create_table(
        "payroll_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("kind", kind, nullable=False, server_default="BANK_ADVICE"),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "content_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/pdf",
        ),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        # The in-DB fallback used only when S3 is not configured, mirroring
        # workspace_files and screenshots so local runs need no AWS.
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["uploaded_by"], ["employees.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_payroll_receipts_period_month", "payroll_receipts", ["period_month"])
    op.create_index("ix_payroll_receipts_file_sha256", "payroll_receipts", ["file_sha256"])
    op.create_index(
        "ix_payroll_receipts_month_kind", "payroll_receipts", ["period_month", "kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_receipts_month_kind", table_name="payroll_receipts")
    op.drop_index("ix_payroll_receipts_file_sha256", table_name="payroll_receipts")
    op.drop_index("ix_payroll_receipts_period_month", table_name="payroll_receipts")
    op.drop_table("payroll_receipts")
    sa.Enum(name=_KIND).drop(op.get_bind(), checkfirst=True)
