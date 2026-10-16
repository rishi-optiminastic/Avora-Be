"""eod_settings.digest_sent_on — once-per-day marker for the department digests

The per-department EOD rollup now goes to admins at the send time, while each
person's own report goes only to their reporting manager. The scheduler ticks
every 15 minutes and re-flushes any still-pending draft, so the digest run needs
a marker to fire once a day rather than on every tick until midnight.

Nullable with no server default: NULL means "never sent", so the first tick at or
after the send time mails the digests and stamps the local date.

Revision ID: d7f1b3c5e9a2
Revises: b2d4f6a8c0e1
Create Date: 2026-10-12 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f1b3c5e9a2"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("eod_settings", sa.Column("digest_sent_on", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("eod_settings", "digest_sent_on")
