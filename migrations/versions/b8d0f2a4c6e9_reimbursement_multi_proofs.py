"""Multiple named proofs per reimbursement.

A claim usually needs more than one document (invoice + toll slip + boarding
pass), each named by the claimant so a reviewer can tell them apart without
opening every file. Replaces the four single-receipt columns on `reimbursements`
with a child table.

Any invoice already attached is carried across before the old columns are
dropped, labelled from its filename, so nothing uploaded so far is lost.

Revision ID: b8d0f2a4c6e9
Revises: a7c9e1b3d5f7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d0f2a4c6e9"
down_revision = "a7c9e1b3d5f7"
branch_labels = None
depends_on = None

_OLD_COLUMNS = (
    "receipt_object_key",
    "receipt_filename",
    "receipt_content_type",
    "receipt_content",
)


def upgrade() -> None:
    op.create_table(
        "reimbursement_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "reimbursement_id",
            sa.Uuid(),
            sa.ForeignKey("reimbursements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column(
            "content_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_reimbursement_receipts_reimbursement_id",
        "reimbursement_receipts",
        ["reimbursement_id"],
    )

    # Carry across whatever is already attached. `split_part(..., '.', 1)` gives a
    # readable label from the filename; claims with nothing attached are skipped.
    op.execute(
        sa.text(
            """
            INSERT INTO reimbursement_receipts (
                id, reimbursement_id, label, filename, content_type,
                size_bytes, object_key, content, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                r.id,
                COALESCE(NULLIF(split_part(r.receipt_filename, '.', 1), ''), 'Invoice'),
                r.receipt_filename,
                COALESCE(r.receipt_content_type, 'application/octet-stream'),
                COALESCE(octet_length(r.receipt_content), 0),
                r.receipt_object_key,
                r.receipt_content,
                r.created_at,
                r.updated_at
            FROM reimbursements r
            WHERE r.receipt_object_key IS NOT NULL OR r.receipt_content IS NOT NULL
            """
        )
    )

    for column in _OLD_COLUMNS:
        op.drop_column("reimbursements", column)


def downgrade() -> None:
    op.add_column(
        "reimbursements", sa.Column("receipt_object_key", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "reimbursements", sa.Column("receipt_filename", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "reimbursements", sa.Column("receipt_content_type", sa.String(length=128), nullable=True)
    )
    op.add_column("reimbursements", sa.Column("receipt_content", sa.LargeBinary(), nullable=True))
    # Only the earliest proof fits back into the single-receipt shape.
    op.execute(
        sa.text(
            """
            UPDATE reimbursements r
            SET receipt_object_key = k.object_key,
                receipt_filename   = k.filename,
                receipt_content_type = k.content_type,
                receipt_content    = k.content
            FROM (
                SELECT DISTINCT ON (reimbursement_id)
                    reimbursement_id, object_key, filename, content_type, content
                FROM reimbursement_receipts
                ORDER BY reimbursement_id, created_at
            ) k
            WHERE k.reimbursement_id = r.id
            """
        )
    )
    op.drop_index("ix_reimbursement_receipts_reimbursement_id", "reimbursement_receipts")
    op.drop_table("reimbursement_receipts")
