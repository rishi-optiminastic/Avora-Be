"""Payout verification: the accounts team's bank payout file, checked against payroll.

- `payout_uploads`: one row per uploaded payout file, with its verification totals.
- `payout_rows`: every payment line in an upload (amount in minor units, UTR,
  match status; only the last four digits of a bank account).
- `payout_mismatches`: each disagreement with payroll, resolvable with a note.

Enum labels are UPPERCASE member names (see tests/test_migration_enum_labels.py).
HR is flagged with the existing SYSTEM notification kind, so no enum is altered.

Revision ID: a4c6e8f0b2d5
Revises: f3b5d7e9a1c4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4c6e8f0b2d5"
down_revision = "f3b5d7e9a1c4"
branch_labels = None
depends_on = None

_ENUMS = {
    "payoutrunkind": ("SALARY", "SALARY_ONLY", "REIMBURSEMENTS"),
    "payoutrowstatus": ("MATCHED", "UNMATCHED", "FAILED"),
    "payoutmismatchkind": (
        "PAID_NOT_EXPECTED",
        "EXPECTED_NOT_PAID",
        "AMOUNT_MISMATCH",
        "DUPLICATE_PAYMENT",
        "UNMATCHED_ROW",
        "ACCOUNT_MISMATCH",
    ),
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


def _employee_fk(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["employees.id"], name=f"fk_{table}_{column}_employees", ondelete="SET NULL"
    )


def _upload_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["upload_id"],
        ["payout_uploads.id"],
        name=f"fk_{table}_upload_id_payout_uploads",
        ondelete="CASCADE",
    )


def _create_uploads() -> None:
    op.create_table(
        "payout_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("kind", _enum("payoutrunkind"), nullable=False, server_default="SALARY"),
        sa.Column("filename", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("file_type", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("file_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("identifier_columns", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("paid_total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mismatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _employee_fk("uploaded_by", "payout_uploads"),
        sa.PrimaryKeyConstraint("id", name="pk_payout_uploads"),
    )
    op.create_index(
        "ix_payout_uploads_period_month", "payout_uploads", ["period_month"], unique=False
    )


def _create_rows() -> None:
    op.create_table(
        "payout_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("matched_by", sa.String(length=16), nullable=True),
        sa.Column("employee_number", sa.String(length=64), nullable=True),
        sa.Column("account_last4", sa.String(length=4), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("payee_name", sa.String(length=256), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("bank_status", sa.String(length=32), nullable=True),
        sa.Column("status", _enum("payoutrowstatus"), nullable=False, server_default="UNMATCHED"),
        *_timestamps(),
        _upload_fk("payout_rows"),
        _employee_fk("employee_id", "payout_rows"),
        sa.PrimaryKeyConstraint("id", name="pk_payout_rows"),
    )
    op.create_index("ix_payout_rows_upload_id", "payout_rows", ["upload_id"], unique=False)
    op.create_index("ix_payout_rows_employee_id", "payout_rows", ["employee_id"], unique=False)


def _create_mismatches() -> None:
    op.create_table(
        "payout_mismatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("kind", _enum("payoutmismatchkind"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("row_id", sa.Uuid(), nullable=True),
        sa.Column("expected_minor", sa.BigInteger(), nullable=True),
        sa.Column("paid_minor", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolution_note", sa.String(length=1000), nullable=True),
        *_timestamps(),
        _upload_fk("payout_mismatches"),
        _employee_fk("employee_id", "payout_mismatches"),
        _employee_fk("resolved_by", "payout_mismatches"),
        sa.ForeignKeyConstraint(
            ["row_id"],
            ["payout_rows.id"],
            name="fk_payout_mismatches_row_id_payout_rows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payout_mismatches"),
    )
    op.create_index(
        "ix_payout_mismatches_upload_id", "payout_mismatches", ["upload_id"], unique=False
    )
    op.create_index(
        "ix_payout_mismatches_employee_id", "payout_mismatches", ["employee_id"], unique=False
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name in _ENUMS:
        _enum(name).create(bind, checkfirst=True)
    _create_uploads()
    _create_rows()
    _create_mismatches()


def downgrade() -> None:
    op.drop_table("payout_mismatches")
    op.drop_table("payout_rows")
    op.drop_table("payout_uploads")
    bind = op.get_bind()
    for name in _ENUMS:
        postgresql.ENUM(name=name, create_type=False).drop(bind, checkfirst=True)
