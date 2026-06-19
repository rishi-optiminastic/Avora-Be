"""screenshot OCR: ocr_status + ocr_text

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-17 16:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ocr_status = sa.Enum("PENDING", "DONE", "FAILED", name="ocrstatus")
    ocr_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "screenshots",
        sa.Column("ocr_status", ocr_status, nullable=False, server_default="PENDING"),
    )
    op.add_column("screenshots", sa.Column("ocr_text", sa.Text(), nullable=True))
    op.create_index("ix_screenshots_ocr_status", "screenshots", ["ocr_status"])


def downgrade() -> None:
    op.drop_index("ix_screenshots_ocr_status", table_name="screenshots")
    op.drop_column("screenshots", "ocr_text")
    op.drop_column("screenshots", "ocr_status")
    sa.Enum(name="ocrstatus").drop(op.get_bind(), checkfirst=True)
