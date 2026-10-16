"""Weighted scores for forms with weighted rating questions. Pure, so the rules
are unit-tested directly.

A weighted question is worth `weight` points out of 100: a rating of r out of 5
scores (r / 5) * weight. A response's total is the sum over its weighted
questions, out of 100; an optional weighted question left unanswered scores 0.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from app.models.form import FormField, FormFieldType
from app.schemas.form import RATING_MAX, WEIGHT_TOTAL, AnswerRead

_DECIMALS = 2


def weighted_score(rating: int | None, weight: int | None) -> float | None:
    if rating is None or weight is None:
        return None
    return round(rating / RATING_MAX * weight, _DECIMALS)


def weights_problem(fields: Iterable[FormField]) -> str | None:
    """Why this form's weights can't be published, or None when they can.

    A form without weights is fine. A form with any weight must have them add
    up to exactly 100, or the "out of 100" total would mean nothing."""
    weights = [f.weight for f in fields if f.type is FormFieldType.RATING and f.weight is not None]
    if not weights:
        return None
    total = sum(weights)
    if total != WEIGHT_TOTAL:
        return (
            f"The question weights add up to {total}%. "
            f"Make them add up to exactly {WEIGHT_TOTAL}% before publishing."
        )
    return None


def field_weights(fields: Sequence[FormField]) -> dict[uuid.UUID, int]:
    return {
        f.id: f.weight for f in fields if f.type is FormFieldType.RATING and f.weight is not None
    }


def score_answers(
    weights: dict[uuid.UUID, int], answers: Sequence[AnswerRead]
) -> tuple[list[AnswerRead], float | None]:
    """The answers with their weighted scores filled in, and the total out of
    100 (None when the form has no weighted questions)."""
    if not weights:
        return list(answers), None
    scored = [
        a.model_copy(update={"weighted_score": weighted_score(a.rating, weights.get(a.field_id))})
        for a in answers
    ]
    total = sum(a.weighted_score or 0.0 for a in scored)
    return scored, round(total, _DECIMALS)
