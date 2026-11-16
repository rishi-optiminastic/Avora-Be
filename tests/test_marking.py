"""Marking a CPD assessment - the rules, tested directly.

Pure functions, so these need no database and can be exhaustive about the
cases that decide whether a mark is fair.
"""

from __future__ import annotations

import pytest

from app.core.marking import clean_choice, is_right, mark

#: (id, options, correct, points)
_SINGLE = ("q1", ["a", "b", "c"], [1], 1)
_MULTI = ("q2", ["a", "b", "c", "d"], [0, 2], 2)


class TestCleanChoice:
    def test_sorts_and_deduplicates(self) -> None:
        """A set of picks, not a list: the UI may send them in click order."""
        assert clean_choice([2, 0, 2], 3) == [0, 2]

    def test_drops_an_index_outside_the_question(self) -> None:
        # A stale tab or a tampered payload. Dropping it makes the answer
        # score zero, which beats failing the whole submission.
        assert clean_choice([0, 99, -1], 3) == [0]

    def test_nothing_picked_is_an_empty_list_not_an_error(self) -> None:
        assert clean_choice([], 3) == []


class TestIsRight:
    def test_a_single_answer_matches_its_key(self) -> None:
        assert is_right([1], [1], 3) is True

    def test_a_different_single_answer_does_not(self) -> None:
        assert is_right([0], [1], 3) is False

    def test_a_multi_answer_needs_every_correct_option(self) -> None:
        """Three of four right is not 75% right - it is a wrong answer to
        "which of these apply"."""
        assert is_right([0], [0, 2], 4) is False

    def test_a_multi_answer_must_not_add_a_wrong_one(self) -> None:
        # Otherwise ticking every box scores the paper.
        assert is_right([0, 1, 2], [0, 2], 4) is False

    def test_a_multi_answer_matching_exactly_is_right(self) -> None:
        assert is_right([2, 0], [0, 2], 4) is True

    def test_an_empty_key_can_never_be_satisfied(self) -> None:
        """A question with no right answer is broken. Scoring it correct for
        everybody would hide that instead of showing it."""
        assert is_right([], [], 3) is False
        assert is_right([0], [], 3) is False

    def test_a_key_pointing_outside_the_options_is_ignored(self) -> None:
        # Leaves [1], so picking 1 is right and picking 1 and 9 is not.
        assert is_right([1], [1, 9], 3) is True
        assert is_right([1, 2], [1, 9], 3) is False


class TestMark:
    def test_a_perfect_paper(self) -> None:
        result = mark([_SINGLE, _MULTI], {"q1": [1], "q2": [0, 2]}, pass_mark=60)

        assert result.score == 3
        assert result.max_score == 3
        assert result.percentage == 100
        assert result.passed is True
        assert [m.is_correct for m in result.marks] == [True, True]

    def test_points_weight_the_harder_question(self) -> None:
        """Getting only the 2-point question right beats only the 1-point."""
        easy = mark([_SINGLE, _MULTI], {"q1": [1]}, pass_mark=60)
        hard = mark([_SINGLE, _MULTI], {"q2": [0, 2]}, pass_mark=60)

        assert easy.score == 1
        assert hard.score == 2
        assert hard.percentage > easy.percentage

    def test_a_question_nobody_answered_is_wrong_not_skipped(self) -> None:
        """Dropping it from the denominator would mean the less you attempt,
        the better you score."""
        result = mark([_SINGLE, _MULTI], {"q1": [1]}, pass_mark=60)

        assert result.max_score == 3, "still out of three"
        assert result.score == 1
        assert result.marks[1].is_correct is False
        assert result.marks[1].chosen == []

    def test_an_answer_to_a_question_not_on_the_paper_is_ignored(self) -> None:
        # The submission names a question the test does not have: it cannot
        # earn points, and it must not crash the marking either.
        result = mark([_SINGLE], {"q1": [1], "ghost": [0]}, pass_mark=60)

        assert result.score == 1
        assert len(result.marks) == 1

    @pytest.mark.parametrize(
        ("pass_mark", "expected"),
        [(0, True), (33, True), (34, False), (100, False)],
    )
    def test_the_pass_mark_is_inclusive(self, pass_mark: int, expected: bool) -> None:
        """Exactly the pass mark passes. 1 of 3 is 33.33%."""
        result = mark([_SINGLE, _MULTI], {"q1": [1]}, pass_mark=pass_mark)
        assert result.passed is expected

    def test_an_empty_paper_scores_zero_and_passes_nobody(self) -> None:
        """Dividing would crash, and "100% of nothing" is a worse answer."""
        result = mark([], {}, pass_mark=0)

        assert result.score == 0
        assert result.max_score == 0
        assert result.percentage == 0
        assert result.passed is False, "a pass mark of 0 must not pass an empty test"

    def test_a_question_worth_nothing_cannot_be_failed_into_a_zero_total(self) -> None:
        # Worth 0, so it adds nothing either way - but the paper is still
        # marked rather than dividing by zero.
        result = mark([("q", ["a", "b"], [0], 0), _SINGLE], {"q1": [1]}, pass_mark=50)

        assert result.max_score == 1
        assert result.score == 1
        assert result.passed is True

    def test_negative_points_are_clamped_rather_than_subtracting(self) -> None:
        """A typo in the editor must not make a question worth minus marks."""
        result = mark([("q", ["a"], [0], -5)], {"q": [0]}, pass_mark=0)

        assert result.max_score == 0
        assert result.score == 0
