"""EOD report metrics + screenshot monitors/vision columns

Adds:
  • eod_reports.metrics      — worked/active minutes + tasks_done for the email card,
                               captured at generation so the send path never recomputes.
  • screenshots.monitors     — per-monitor rectangles within a combined multi-monitor
                               capture, so the OCR worker can crop + OCR each screen.
  • screenshots.vision_json  — cached structured visual context for sampled frames
                               (apps/projects/working_on/detail), filled by the EOD
                               vision pass so a re-run never re-bills the vision model.

All three default to empty/null and are backfilled by `server_default`, so existing
rows stay valid with no data migration.

Revision ID: a7c1d2e3f4b5
Revises: e0d5e771a9c2
Create Date: 2026-06-30 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c1d2e3f4b5"
down_revision: str | None = "e0d5e771a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eod_reports",
        sa.Column(
            "metrics",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "screenshots",
        sa.Column(
            "monitors",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "screenshots",
        sa.Column("vision_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("screenshots", "vision_json")
    op.drop_column("screenshots", "monitors")
    op.drop_column("eod_reports", "metrics")
