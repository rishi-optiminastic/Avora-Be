"""Marking a CPD assessment. Pure, so every rule is unit-tested directly.

The rules, and why each is the way it is:

* **A MULTI question is all-or-nothing.** Picking three of four correct
  options is not 75% right; it is a wrong answer to "which of these apply".
  Partial credit would also let somebody tick every box and score most of
  the paper.
* **Order and duplicates do not matter.** The taker picks a set, and
  `[2, 0]`, `[0, 2]` and `[0, 2, 2]` are the same set.
* **An option index outside the question is wrong, not an error.** A stale
  tab or a tampered payload should score zero, not 500 the whole submission.
* **A question nobody answered is wrong.** Not skipped, not excluded from the
  total: a test where unanswered questions vanish from the denominator means
  the less you attempt the better you do.
* **A test with no points at all scores 0 of 0 and passes nobody.** Dividing
  would be a crash, and "100%" on an empty paper is a worse answer than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_DECIMALS = 2


@dataclass(frozen=True)
class Marked:
    """One question, marked."""

    question_id: object
    chosen: list[int]
    is_correct: bool
    points_awarded: float


@dataclass(frozen=True)
class Result:
    """A whole paper, marked."""

    marks: list[Marked]
    score: float
    max_score: float
    percentage: float
    passed: bool


def clean_choice(chosen: Sequence[int], option_count: int) -> list[int]:
    """The taker's picks as a sorted, de-duplicated, in-range list.

    Out-of-range indexes are DROPPED rather than rejected: the comparison
    below then fails naturally and the answer scores zero, which is the right
    outcome for a stale tab without failing the rest of the paper with it.
    """
    return sorted({i for i in chosen if 0 <= i < option_count})


def is_right(chosen: Sequence[int], correct: Sequence[int], option_count: int) -> bool:
    """Whether this answer matches the key exactly.

    Set equality, so order and duplicates do not matter and a MULTI question
    needs every correct option and no incorrect one. An empty key can never
    be satisfied - a question with no right answer is a broken question, and
    scoring it correct for everybody would hide that.
    """
    key = sorted({i for i in correct if 0 <= i < option_count})
    if not key:
        return False
    return clean_choice(chosen, option_count) == key


def mark(
    questions: Sequence[tuple[object, Sequence[str], Sequence[int], int]],
    answers: dict[object, Sequence[int]],
    pass_mark: int,
) -> Result:
    """Mark a submission.

    `questions` is (id, options, correct, points) per question - a plain tuple
    rather than the ORM row, so this module stays pure and the tests do not
    need a database.

    Every question in `questions` is marked, including ones missing from
    `answers`: a question nobody answered is wrong, and dropping it from the
    denominator would mean the less you attempt the better you score.
    """
    marks: list[Marked] = []
    score = 0.0
    max_score = 0.0
    for question_id, options, correct, points in questions:
        worth = float(max(0, points))
        max_score += worth
        chosen = clean_choice(answers.get(question_id, []), len(options))
        correct_answer = is_right(chosen, correct, len(options))
        awarded = worth if correct_answer else 0.0
        score += awarded
        marks.append(
            Marked(
                question_id=question_id,
                chosen=chosen,
                is_correct=correct_answer,
                points_awarded=awarded,
            )
        )

    # A paper worth nothing scores 0 of 0 and passes nobody. "100% of zero"
    # would hand everyone a pass on an empty test.
    percentage = round(score / max_score * 100, _DECIMALS) if max_score > 0 else 0.0
    return Result(
        marks=marks,
        score=round(score, _DECIMALS),
        max_score=round(max_score, _DECIMALS),
        percentage=percentage,
        passed=max_score > 0 and percentage >= pass_mark,
    )
