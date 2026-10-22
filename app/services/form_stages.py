"""Forms that two people fill in: the employee's half, then their manager's.

The case this exists for is an appraisal - the person writes their own
assessment and their reporting manager then writes the rest, on the SAME form,
so both halves end up in one set of answers rather than two documents nobody
lines up afterwards.

Pure, so every rule is unit-tested directly. The shape of it:

* A form is two-stage **if and only if some field says so**. There is no flag
  on the form to drift out of step with its questions.
* The assignment stays PENDING across the handover. It is one form being filled
  in by two people, not two forms; only `assignee_id` and `stage` move.
* Each stage sees and answers only its own fields. The manager cannot overwrite
  what the employee wrote, because those fields are not theirs to submit.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.form import STAGE_ORDER, FormField, FormFieldType, FormStage


def is_staged(fields: Sequence[FormField]) -> bool:
    """Whether this form is filled in by two people."""
    return any(field.stage is not None for field in fields)


def answerable(fields: Sequence[FormField]) -> list[FormField]:
    """Questions only - section headings are not answered."""
    return [field for field in fields if field.type is not FormFieldType.SECTION]


def stage_has_questions(fields: Sequence[FormField], stage: FormStage) -> bool:
    """Whether a stage has anything to answer.

    A stage made only of section headings would otherwise be handed to someone
    with nothing to do and no way to move it on.
    """
    return bool(answerable([f for f in fields if f.stage is stage]))


def stages_used(fields: Sequence[FormField]) -> list[FormStage]:
    """The stages this form actually uses, in filling order.

    A stage counts only if it has something to ANSWER. A form whose questions
    are all marked `manager` is legitimate - the manager fills the whole thing -
    and starts there rather than waiting at an employee stage with nothing in
    it. The same rule stops a stage of nothing but section headings being
    handed to someone who then has an empty form and no way to move it on.
    """
    return [stage for stage in STAGE_ORDER if stage_has_questions(fields, stage)]


def first_stage(fields: Sequence[FormField]) -> FormStage | None:
    """Where a new assignment for this form starts, or None if not staged."""
    used = stages_used(fields)
    return used[0] if used else None


def next_stage(fields: Sequence[FormField], current: FormStage | None) -> FormStage | None:
    """The stage after `current`, or None when this was the last one.

    None is what tells the caller to finish the assignment rather than hand it
    on, so it has to mean exactly "nobody else owes anything".
    """
    if current is None:
        return None
    used = stages_used(fields)
    if current not in used:
        return None
    position = used.index(current)
    return used[position + 1] if position + 1 < len(used) else None


def fields_for_stage(fields: Sequence[FormField], stage: FormStage | None) -> list[FormField]:
    """The questions this stage owns.

    Sections come along with whichever stage they head, so each person sees
    their own half laid out the way it was written rather than a bare list of
    questions. A form with no stages gives everything to its one assignee.
    """
    if stage is None:
        return list(fields)
    return [field for field in fields if field.stage is stage]
