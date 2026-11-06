"""Merge the two probation review forms into one two-stage form

The probation review went out as three independent forms that all fired at the
same moment, 21 days before the probation end date: the employee's
self-assessment, the manager's evaluation, and HR's. Nothing linked them. The
manager could - and in practice did - receive their form before the employee
had opened theirs, submit it first, and never see a word the employee wrote,
because `_may_open_answers` lets only the assignee or HR/admin read a form.

The checklist made that look like a sequence it never was: step 4 ("Share
probation form to reporting manager") sat green above step 3 ("Employee
completes self-assessment"), still open.

`FormStage` already existed for exactly this shape - "an appraisal where the
person writes their own half and their reporting manager then writes the rest,
on the SAME form, so the two halves sit side by side in one set of answers" -
and probation simply never used it. This migration makes it use it:

* the self-assessment's questions become the EMPLOYEE stage,
* the manager evaluation's questions move onto that same form as the MANAGER
  stage, keeping their order and their weights,
* the now-empty manager form is ARCHIVED so it stops being sent, and
* any manager-form assignment still waiting is cancelled.

The handover then does the work the checklist was describing: the employee
submits, the assignment stays PENDING and moves to their reporting manager,
who opens it and reads the employee's half above their own questions.

ONLY the HR form stays separate. It is a different person's judgement on a
different timetable, and nothing about it waits on the other two.

SAFETY. This runs only when the shape is unambiguous - exactly one PUBLISHED
form on each of the two triggers. A fresh database has none and this is a
no-op; an org that has published several versions is left alone rather than
guessed at. Assignments with answers already recorded are never cancelled: the
`NOT EXISTS` guard keeps anyone's submitted words.

Revision ID: c4e6a8b0d2f4
Revises: b2d4f6a8c0e2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e6a8b0d2f4"
down_revision: str | None = "b2d4f6a8c0e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy persists StrEnum MEMBER NAMES, so every label here is uppercase.
_SELF = "PROBATION_SELF"
_MANAGER = "PROBATION_MANAGER"
_PUBLISHED = "PUBLISHED"
_ARCHIVED = "ARCHIVED"
_PENDING = "PENDING"
_NEEDS_MANAGER = "NEEDS_MANAGER"
_CANCELLED = "CANCELLED"

_MERGED_TITLE = "Probation Review"


def _one_published(bind: sa.Connection, trigger: str) -> sa.Row[tuple[str, str]] | None:
    """The single published form on this trigger, or None if 0 or many.

    Many means somebody has published more than one version and a migration
    cannot know which is live; leaving it alone is the only honest answer.
    """
    rows = bind.execute(
        sa.text(
            "SELECT id, title FROM forms"
            " WHERE trigger = :trigger AND status = :status"
            " ORDER BY created_at"
        ),
        {"trigger": trigger, "status": _PUBLISHED},
    ).all()
    return rows[0] if len(rows) == 1 else None


def upgrade() -> None:
    bind = op.get_bind()
    employee_form = _one_published(bind, _SELF)
    manager_form = _one_published(bind, _MANAGER)
    if employee_form is None or manager_form is None:
        return  # fresh database, or a shape this cannot safely decide

    # 1. The self-assessment's questions become the employee's half. Done
    #    first: a form is two-stage if and only if some field says so, and
    #    leaving these null while the manager's arrive staged would briefly
    #    make the manager's half the FIRST stage (`stages_used` skips a stage
    #    with nothing to answer), handing the whole form to the wrong person.
    bind.execute(
        sa.text("UPDATE form_fields SET stage = 'EMPLOYEE' WHERE form_id = :form_id"),
        {"form_id": employee_form.id},
    )

    # 2. The manager's questions move across, after the employee's.
    #    `uq_form_fields_position` is on (form_id, position), so they are
    #    shifted clear of the positions already in use rather than renumbered
    #    from scratch - which keeps the order HR wrote them in.
    offset = (
        bind.execute(
            sa.text("SELECT COALESCE(MAX(position), -1) + 1 FROM form_fields WHERE form_id = :f"),
            {"f": employee_form.id},
        ).scalar_one()
    )
    bind.execute(
        sa.text(
            "UPDATE form_fields"
            " SET form_id = :to_form, stage = 'MANAGER', position = position + :offset"
            " WHERE form_id = :from_form"
        ),
        {"to_form": employee_form.id, "from_form": manager_form.id, "offset": offset},
    )

    # 3. The form is no longer only a self-assessment.
    bind.execute(
        sa.text("UPDATE forms SET title = :title WHERE id = :id"),
        {"title": _MERGED_TITLE, "id": employee_form.id},
    )

    # 4. Stop the empty manager form going out. Archived, never deleted: its
    #    old assignments and answers stay readable.
    bind.execute(
        sa.text("UPDATE forms SET status = :archived WHERE id = :id"),
        {"archived": _ARCHIVED, "id": manager_form.id},
    )

    # 5. Cancel manager assignments nobody has answered. One that HAS answers
    #    is left exactly as it is - a half-finished review is still a record.
    bind.execute(
        sa.text(
            "UPDATE form_assignments SET status = :cancelled"
            " WHERE form_id = :form_id AND status IN (:pending, :needs_manager)"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM form_responses r WHERE r.assignment_id = form_assignments.id"
            " )"
        ),
        {
            "cancelled": _CANCELLED,
            "form_id": manager_form.id,
            "pending": _PENDING,
            "needs_manager": _NEEDS_MANAGER,
        },
    )


def downgrade() -> None:
    """Split the manager's half back onto its own published form.

    The cancelled assignments are NOT revived: they were cancelled because
    nobody had answered them, and the sweep will issue fresh ones on the next
    run for anyone still inside their review window.
    """
    bind = op.get_bind()
    employee_form = _one_published(bind, _SELF)
    rows = bind.execute(
        sa.text(
            "SELECT id FROM forms WHERE trigger = :trigger AND status = :status ORDER BY created_at"
        ),
        {"trigger": _MANAGER, "status": _ARCHIVED},
    ).all()
    if employee_form is None or len(rows) != 1:
        return
    manager_form_id = rows[0].id

    offset = (
        bind.execute(
            sa.text(
                "SELECT COALESCE(MIN(position), 0) FROM form_fields"
                " WHERE form_id = :f AND stage = 'MANAGER'"
            ),
            {"f": employee_form.id},
        ).scalar_one()
    )
    bind.execute(
        sa.text(
            "UPDATE form_fields"
            " SET form_id = :to_form, stage = NULL, position = position - :offset"
            " WHERE form_id = :from_form AND stage = 'MANAGER'"
        ),
        {"to_form": manager_form_id, "from_form": employee_form.id, "offset": offset},
    )
    bind.execute(
        sa.text("UPDATE form_fields SET stage = NULL WHERE form_id = :f"),
        {"f": employee_form.id},
    )
    bind.execute(
        sa.text("UPDATE forms SET status = :published WHERE id = :id"),
        {"published": _PUBLISHED, "id": manager_form_id},
    )
    bind.execute(
        sa.text("UPDATE forms SET title = :title WHERE id = :id"),
        {"title": "Probation Self-Assessment", "id": employee_form.id},
    )
