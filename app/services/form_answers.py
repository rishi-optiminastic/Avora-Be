"""Check a submission against the questions it answers. Pure, so every rule
is unit-tested directly."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.exceptions import ValidationError
from app.models.form import FormField, FormFieldType
from app.schemas.form import AnswerWrite


def check_answers(
    fields: Sequence[FormField], answers: Sequence[AnswerWrite]
) -> list[dict[str, object]]:
    """Validate a submission against the questions; return the rows to store.

    Every required question needs an answer of its own kind. A rating's written
    comment is optional: the score is the must-have, the words are welcome."""
    by_id = {a.field_id: a for a in answers}
    known = {f.id for f in fields}
    if any(field_id not in known for field_id in by_id):
        raise ValidationError("An answer refers to a question that is not on this form.")
    rows: list[dict[str, object]] = []
    for field in fields:
        if field.type is FormFieldType.SECTION:
            continue
        answer = by_id.get(field.id)
        row = _answer_row(field, answer)
        if row is None:
            if field.required:
                raise ValidationError(f"Please answer: {field.label[:80]}")
            continue
        rows.append(row)
    return rows


def _answer_row(field: FormField, answer: AnswerWrite | None) -> dict[str, object] | None:
    if answer is None:
        return None
    text = (answer.text or "").strip() or None
    if field.type is FormFieldType.RATING:
        if answer.rating is None:
            return None
        return {
            "field_id": field.id,
            "rating": answer.rating,
            "text": text if field.with_comment else None,
        }
    if field.type is FormFieldType.CHOICE:
        if answer.choice is None:
            return None
        if answer.choice not in (field.options or []):
            raise ValidationError(f"Pick one of the listed options for: {field.label[:80]}")
        return {"field_id": field.id, "choice": answer.choice}
    return {"field_id": field.id, "text": text} if text else None
