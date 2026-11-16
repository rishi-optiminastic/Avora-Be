"""Pause every recurring series that predates recurrence ever running.

`bb7f699e8c77` added `recurrence_active` with `server_default=true`, which
quietly opted 511 existing tasks into regeneration. That default was wrong,
and it is only harmless while the scheduler is off.

`tasks.cadence` shipped years ago as a LABEL nothing acted on, so people used
it to describe their work rather than to schedule it. On the live database the
rows carrying a recurring cadence read:

    DAILY    "Induction - Aditi"
    DAILY    "Send documents to Shalu for Aditi"
    DAILY    "Sandesh - restocked biscuits in the pantry"
    MONTHLY  "Create monthly reports - sep"

Those are one-off jobs, finished, named after the person or month they
belonged to. They became instructions the moment a scheduler started reading
them: 117 tasks on the first sweep, 92 of them reviving series nobody had
touched in over a month, "sep" included, forever.

Nobody consented to that - a default did. So recurrence becomes opt-in for
everything that already exists, and the edit dialog's "Keep creating the next
one" turns a series back on deliberately, one at a time.

This creates and deletes nothing. It flips a flag on tasks that have never
generated an instance, and `series_id IS NULL` asserts exactly that.

Reversibility: "was this paused by a person or by this migration?" is not
recoverable from the row afterwards, and guessing it wrong would re-enable a
series somebody had deliberately stopped. So upgrade records the ids it
touched in a backup table and downgrade restores exactly those, then drops
it. An upgrade that is never reverted leaves that one small table behind;
that is the price of being able to undo this precisely.

Revision ID: a1c3e5f7b9d4
Revises: bb7f699e8c77
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c3e5f7b9d4"
down_revision = "bb7f699e8c77"
branch_labels = None
depends_on = None

# Literal SQL, no interpolation: §5.6 forbids building a statement with string
# formatting, and a migration reads better as the statement it actually runs.
# The DB enum stores the member NAMES, uppercased - not the values, so this
# matches 'DAILY' and never 'daily'.
#
# `series_id IS NULL` is the load-bearing clause: it limits this to series the
# scheduler has never acted on. Once an instance exists the flag is a live
# setting somebody relies on, not an inherited default.
_SNAPSHOT = """
CREATE TABLE _recurrence_pause_a1c3e5f7b9d4 AS
SELECT id FROM tasks
 WHERE cadence::text IN ('DAILY', 'WEEKLY', 'MONTHLY')
   AND series_id IS NULL
   AND recurrence_active
"""

_PAUSE = """
UPDATE tasks SET recurrence_active = false, updated_at = now()
 WHERE id IN (SELECT id FROM _recurrence_pause_a1c3e5f7b9d4)
"""

# Guarded, not bare: a downgrade run twice - or against a database whose
# upgrade never ran - must be a no-op, and a bare UPDATE naming a missing
# table raises instead. The DROP sits inside the same guard so the restore and
# the table can never come apart.
_RESTORE = """
DO $$ BEGIN
  IF to_regclass('_recurrence_pause_a1c3e5f7b9d4') IS NOT NULL THEN
    UPDATE tasks SET recurrence_active = true, updated_at = now()
     WHERE id IN (SELECT id FROM _recurrence_pause_a1c3e5f7b9d4);
    DROP TABLE _recurrence_pause_a1c3e5f7b9d4;
  END IF;
END $$
"""


def upgrade() -> None:
    # CREATE ... AS SELECT first, so the backup holds precisely the rows the
    # UPDATE then changes - not a predicate re-evaluated later against data
    # that has since moved.
    op.execute(sa.text(_SNAPSHOT))
    op.execute(sa.text(_PAUSE))


def downgrade() -> None:
    op.execute(sa.text(_RESTORE))
