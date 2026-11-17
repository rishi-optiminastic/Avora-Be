"""The pure playbook rules: rendering a step into a task, and its due date.

Cases are taken from the real titles on the live database, because that is
what the feature has to reproduce without a human retyping them:
"Induction - Aditi", "Send documents to Shalu for Aditi",
"Pre onboarding of Sahil for Full stack".
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.playbook import (
    MAX_OFFSET_DAYS,
    MIN_OFFSET_DAYS,
    PLACEHOLDERS,
    Subject,
    due_on,
    is_due,
    month_anchor,
    opens_on,
    period_key,
    render,
    unknown_placeholders,
)
from app.models.playbook import PlaybookTrigger

ADITI = Subject(
    full_name="Aditi Sharma",
    department="Social Media",
    job_title="SM Intern",
    hire_date=date(2026, 10, 19),
)


class TestRender:
    def test_the_real_onboarding_titles(self) -> None:
        assert render("Induction - {name}", ADITI) == "Induction - Aditi Sharma"
        assert (
            render("Send documents to Shalu for {first_name}", ADITI)
            == "Send documents to Shalu for Aditi"
        )
        assert (
            render("Pre onboarding of {first_name} for {job_title}", ADITI)
            == "Pre onboarding of Aditi for SM Intern"
        )

    def test_a_step_with_no_placeholder_is_untouched(self) -> None:
        # "Welcome Kit - Lunch" is a real step and names nobody.
        assert render("Welcome Kit - Lunch", ADITI) == "Welcome Kit - Lunch"

    def test_every_supported_placeholder_resolves(self) -> None:
        for placeholder in PLACEHOLDERS:
            out = render("x {" + placeholder + "} y", ADITI)
            assert "{" not in out, f"{placeholder} did not resolve"

    def test_start_date_renders_iso(self) -> None:
        assert render("Day one: {start_date}", ADITI) == "Day one: 2026-10-19"

    def test_a_typo_is_left_standing_not_blanked(self) -> None:
        # Visible in the title, so somebody notices. Blanking would give
        # "Induction - " and read as deliberate.
        assert render("Induction - {nmae}", ADITI) == "Induction - {nmae}"

    def test_an_empty_field_reads_as_prose(self) -> None:
        # A joiner with no department on file must never produce "None" or a
        # gap in a task title somebody has to action.
        bare = Subject(full_name="Sahil")
        assert render("Set up {department} access", bare) == "Set up their department access"
        assert render("Confirm {job_title}", bare) == "Confirm their role"
        assert render("Started {start_date}", bare) == "Started their start date"

    def test_a_blank_name_falls_back_rather_than_vanishing(self) -> None:
        blank = Subject(full_name="   ")
        assert render("Induction - {name}", blank) == "Induction - the new joiner"

    def test_first_name_of_a_single_word_name(self) -> None:
        assert render("{first_name}", Subject(full_name="Fiza")) == "Fiza"

    def test_repeated_placeholder_fills_every_time(self) -> None:
        assert render("{first_name} and {first_name}", ADITI) == "Aditi and Aditi"


class TestUnknownPlaceholders:
    def test_reports_only_the_unfillable_ones(self) -> None:
        assert unknown_placeholders("{name} {nmae} {job_title} {salary}") == ["nmae", "salary"]

    def test_a_clean_template_reports_nothing(self) -> None:
        assert unknown_placeholders("Induction - {name}") == []

    def test_deduplicates(self) -> None:
        assert unknown_placeholders("{oops} {oops}") == ["oops"]


class TestDueOn:
    ANCHOR = date(2026, 10, 19)

    def test_offset_counts_from_the_anchor_not_from_today(self) -> None:
        # The whole point: a run for somebody who started last Monday puts
        # day-3 tasks on the Thursday after THAT Monday.
        assert due_on(self.ANCHOR, 3) == date(2026, 10, 22)

    def test_day_zero_is_the_anchor(self) -> None:
        assert due_on(self.ANCHOR, 0) == self.ANCHOR

    def test_a_negative_offset_lands_before_the_anchor(self) -> None:
        # Pre-boarding: "send the laptop" belongs before day one.
        assert due_on(self.ANCHOR, -5) == date(2026, 10, 14)

    @pytest.mark.parametrize("silly", [3000, 99999])
    def test_a_silly_offset_is_clamped_not_rejected(self, silly: int) -> None:
        # Clamped so one bad step cannot abort a run and leave somebody with
        # two thirds of a checklist.
        assert due_on(self.ANCHOR, silly) == due_on(self.ANCHOR, MAX_OFFSET_DAYS)

    def test_clamped_at_the_bottom_too(self) -> None:
        assert due_on(self.ANCHOR, -99999) == due_on(self.ANCHOR, MIN_OFFSET_DAYS)

    def test_crosses_a_month_and_a_leap_year_correctly(self) -> None:
        assert due_on(date(2028, 2, 27), 3) == date(2028, 3, 1)


class TestIsDue:
    """Whether the new-joiner playbook fires for somebody today.

    Pinned directly, not only through the sweep: the sweep ALSO bounds its
    query by `auto_assign_since` in SQL, so removing the bound here alone
    broke nothing a service test could see. Two independent guards is the
    right design; both need their own test.
    """

    SINCE = date(2026, 10, 1)

    def test_fires_for_somebody_who_joined_after_the_playbook_went_live(self) -> None:
        assert is_due(date(2026, 10, 19), True, self.SINCE, 0, date(2026, 10, 19)) is True

    def test_does_NOT_fire_for_somebody_who_was_already_here(self) -> None:
        # THE backfill guard. Publishing must not onboard the whole roster.
        assert is_due(date(2025, 1, 6), True, self.SINCE, 0, date(2026, 10, 19)) is False

    def test_fires_on_the_boundary_day_itself(self) -> None:
        # Joined exactly when it went live: in, not out. An off-by-one here
        # silently skips the first joiner the playbook was written for.
        assert is_due(self.SINCE, True, self.SINCE, 0, self.SINCE) is True

    def test_waits_for_the_offset(self) -> None:
        assert is_due(date(2026, 10, 19), True, self.SINCE, 3, date(2026, 10, 21)) is False
        assert is_due(date(2026, 10, 19), True, self.SINCE, 3, date(2026, 10, 22)) is True

    def test_never_fires_without_a_start_date(self) -> None:
        # No joining date means no telling a new joiner from somebody added to
        # Avora years late, so nobody is guessed at.
        assert is_due(None, True, self.SINCE, 0, date(2026, 10, 19)) is False

    def test_never_fires_for_somebody_who_has_left(self) -> None:
        assert is_due(date(2026, 10, 19), False, self.SINCE, 0, date(2026, 10, 19)) is False

    def test_an_unarmed_playbook_fires_for_nobody(self) -> None:
        # Fails in the safe direction: no arming date means nobody, not
        # everybody.
        assert is_due(date(2026, 10, 19), True, None, 0, date(2026, 10, 19)) is False

    def test_opens_on_treats_a_missing_offset_as_day_zero(self) -> None:
        assert opens_on(date(2026, 10, 19), None) == date(2026, 10, 19)


class TestMonthlyTriggers:
    """A monthly playbook fires once a MONTH, forever - not once ever.

    That is the whole reason uniqueness is keyed by a period: November's
    close and December's are different runs, and neither may happen twice.
    """

    def test_month_start_fires_on_the_first_and_anchors_there(self) -> None:
        assert month_anchor(PlaybookTrigger.MONTH_START, date(2026, 11, 1), 0) == date(2026, 11, 1)

    def test_month_start_does_not_fire_on_other_days(self) -> None:
        assert month_anchor(PlaybookTrigger.MONTH_START, date(2026, 11, 2), 0) is None
        assert month_anchor(PlaybookTrigger.MONTH_START, date(2026, 11, 30), 0) is None

    def test_month_start_can_wait_a_few_days(self) -> None:
        # Fires on the 4th, but steps still count from the 1st, because that
        # is how "GSTR 1 by the 11th" is actually expressed.
        assert month_anchor(PlaybookTrigger.MONTH_START, date(2026, 11, 4), 3) == date(2026, 11, 1)
        assert month_anchor(PlaybookTrigger.MONTH_START, date(2026, 11, 3), 3) is None

    def test_month_end_fires_on_the_last_day(self) -> None:
        assert month_anchor(PlaybookTrigger.MONTH_END, date(2026, 11, 30), 0) == date(2026, 11, 30)

    def test_month_end_knows_short_months(self) -> None:
        assert month_anchor(PlaybookTrigger.MONTH_END, date(2026, 2, 28), 0) == date(2026, 2, 28)
        assert month_anchor(PlaybookTrigger.MONTH_END, date(2028, 2, 29), 0) == date(2028, 2, 29)
        # The 28th of a leap February is NOT month end.
        assert month_anchor(PlaybookTrigger.MONTH_END, date(2028, 2, 28), 0) is None

    def test_month_end_can_start_early_but_still_anchors_on_the_last_day(self) -> None:
        assert month_anchor(PlaybookTrigger.MONTH_END, date(2026, 11, 27), 3) == date(2026, 11, 30)

    def test_a_one_shot_trigger_has_no_monthly_anchor(self) -> None:
        assert month_anchor(PlaybookTrigger.NEW_JOINER, date(2026, 11, 1), 0) is None


class TestPeriodKey:
    def test_a_one_shot_trigger_keys_on_nothing(self) -> None:
        # "" means once ever: a person is onboarded once and offboarded once.
        assert period_key(PlaybookTrigger.NEW_JOINER, date(2026, 11, 17)) == ""
        assert period_key(PlaybookTrigger.OFFBOARDING, date(2026, 11, 17)) == ""

    def test_a_monthly_trigger_keys_on_its_month(self) -> None:
        assert period_key(PlaybookTrigger.MONTH_END, date(2026, 11, 30)) == "2026-11"
        assert period_key(PlaybookTrigger.MONTH_START, date(2026, 12, 1)) == "2026-12"

    def test_two_months_are_different_runs(self) -> None:
        nov = period_key(PlaybookTrigger.MONTH_END, date(2026, 11, 30))
        dec = period_key(PlaybookTrigger.MONTH_END, date(2026, 12, 31))
        assert nov != dec, "the close must be able to happen again next month"

    def test_the_key_is_zero_padded_so_it_sorts(self) -> None:
        assert period_key(PlaybookTrigger.MONTH_START, date(2026, 3, 1)) == "2026-03"
