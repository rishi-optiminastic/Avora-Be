"""Deciding that work on screen has no task behind it.

This is the only place a machine decides a task should EXIST, so the tests
are about restraint. The numbers they defend were measured on the live
database: of 30,617 OCR'd screenshots the catalog names 45.5%, and 35% of
those matches are ambiguous between two entities. A detector that acted on
every match would be guessing on a third of what it touched.
"""

from __future__ import annotations

import uuid

from app.core.untracked import (
    MIN_CONFIDENCE,
    MIN_SIGHTINGS,
    Sighting,
    mismatched_departments,
    title_for,
    untracked_work,
)

CIRCLE = uuid.uuid4()
AVORA = uuid.uuid4()
NOTHING_TRACKED: frozenset[uuid.UUID] = frozenset()


def seen(entity_id: uuid.UUID, name: str, times: int, confidence: int = 70) -> list[Sighting]:
    """Sightings spread across separate time buckets, which is what real work
    looks like. Clustered sightings are their own test below."""
    return [Sighting(entity_id, name, confidence, None, bucket) for bucket in range(times)]


class TestSustained:
    def test_finds_work_seen_enough_times(self) -> None:
        found = untracked_work(seen(CIRCLE, "Circle", MIN_SIGHTINGS), NOTHING_TRACKED)
        assert [c.name for c in found] == ["Circle"]
        assert found[0].sightings == MIN_SIGHTINGS

    def test_one_glance_is_not_work(self) -> None:
        # Somebody opened a tab. Acting on this is how a task list fills with
        # noise and stops being read.
        assert untracked_work(seen(CIRCLE, "Circle", 1), NOTHING_TRACKED) == []

    def test_just_below_the_floor_creates_nothing(self) -> None:
        assert untracked_work(seen(CIRCLE, "Circle", MIN_SIGHTINGS - 1), NOTHING_TRACKED) == []

    def test_the_floor_is_adjustable_without_touching_the_rule(self) -> None:
        # The cheapest lever if the first real run proves noisy.
        sightings = seen(CIRCLE, "Circle", 4)
        assert untracked_work(sightings, NOTHING_TRACKED, min_sightings=5) == []
        assert len(untracked_work(sightings, NOTHING_TRACKED, min_sightings=4)) == 1


class TestConfidence:
    def test_ignores_ambiguous_matches_entirely(self) -> None:
        # `attribute` subtracts 20 when a second entity scores within 80% of
        # the best. Those 4,903 screenshots must never create a task.
        ambiguous = seen(CIRCLE, "Circle", 10, confidence=MIN_CONFIDENCE - 1)
        assert untracked_work(ambiguous, NOTHING_TRACKED) == []

    def test_accepts_a_match_exactly_on_the_floor(self) -> None:
        on_floor = seen(CIRCLE, "Circle", MIN_SIGHTINGS, confidence=MIN_CONFIDENCE)
        assert len(untracked_work(on_floor, NOTHING_TRACKED)) == 1

    def test_ambiguous_sightings_do_not_count_towards_the_total(self) -> None:
        # Two confident hits plus three shaky ones is still two hits.
        mixed = seen(CIRCLE, "Circle", 2) + seen(CIRCLE, "Circle", 3, confidence=20)
        assert untracked_work(mixed, NOTHING_TRACKED) == []

    def test_reports_the_best_confidence_seen(self) -> None:
        mixed = seen(CIRCLE, "Circle", 2, confidence=50) + seen(CIRCLE, "Circle", 1, confidence=90)
        assert untracked_work(mixed, NOTHING_TRACKED)[0].confidence == 90


class TestAlreadyTracked:
    def test_says_nothing_when_a_task_already_points_at_the_entity(self) -> None:
        # The work IS tracked. Adding a second task for it is the duplication
        # this feature exists to remove, not create.
        assert untracked_work(seen(CIRCLE, "Circle", 9), frozenset({CIRCLE})) == []

    def test_still_reports_the_other_entity(self) -> None:
        both = seen(CIRCLE, "Circle", 5) + seen(AVORA, "Avora", 4)
        found = untracked_work(both, frozenset({CIRCLE}))
        assert [c.name for c in found] == ["Avora"]


class TestOrdering:
    def test_best_evidenced_first(self) -> None:
        # A caller that caps how many tasks it creates must take the strongest.
        both = seen(CIRCLE, "Circle", 4) + seen(AVORA, "Avora", 9)
        assert [c.name for c in untracked_work(both, NOTHING_TRACKED)] == ["Avora", "Circle"]

    def test_confidence_breaks_a_tie(self) -> None:
        both = seen(CIRCLE, "Circle", 4, confidence=60) + seen(AVORA, "Avora", 4, confidence=90)
        assert [c.name for c in untracked_work(both, NOTHING_TRACKED)] == ["Avora", "Circle"]


class TestTitle:
    def test_uses_the_entity_name_and_nothing_from_the_screen(self) -> None:
        assert title_for("Circle") == "Untracked work on Circle"

    def test_trims_and_caps_to_the_column(self) -> None:
        assert title_for("  Circle  ") == "Untracked work on Circle"
        assert len(title_for("x" * 400)) <= 256

    def test_reads_as_machine_written_on_purpose(self) -> None:
        # Somebody scanning their list should be able to tell at a glance that
        # nobody wrote this for them.
        assert title_for("Avora").startswith("Untracked work on")


class TestDepartmentGate:
    """Telling work somebody DOES from a tool they merely use.

    Added because the dry run against live data proved it was needed. With
    only the first three gates the detector proposed, in one afternoon:

        Social Media Intern -> "Untracked work on Avora"
        Finance Manager     -> "Untracked work on Avora"
        HR Manager          -> "Untracked work on Circle"

    Every statement was literally true and every one was useless. Six of ten
    proposals were that mistake.
    """

    ENGINEERING = "Engineering"

    def owned(self, dept: str | None, times: int = 4) -> list[Sighting]:
        return [Sighting(CIRCLE, "Circle", 70, dept, bucket) for bucket in range(times)]

    def test_an_engineer_gets_an_engineering_project(self) -> None:
        found = untracked_work(self.owned(self.ENGINEERING), NOTHING_TRACKED, self.ENGINEERING)
        assert [c.name for c in found] == ["Circle"]

    def test_an_hr_manager_does_NOT_get_the_hr_system_they_use(self) -> None:
        # The exact case from the dry run: Circle is Engineering's product and
        # HR's daily tool. A text match cannot tell those apart; this can.
        assert untracked_work(self.owned(self.ENGINEERING), NOTHING_TRACKED, "Management") == []

    def test_a_finance_person_does_NOT_get_the_dashboard_they_log_into(self) -> None:
        assert untracked_work(self.owned(self.ENGINEERING), NOTHING_TRACKED, "Finance") == []

    def test_an_entity_owned_by_nobody_belongs_to_anybody(self) -> None:
        # A cross-company project ("Thoughtful Vibe" has no department) is
        # real work for whoever touches it.
        for dept in ("Engineering", "Finance", "Digital Marketing", None):
            assert len(untracked_work(self.owned(None), NOTHING_TRACKED, dept)) == 1

    def test_a_misspelt_department_matches_NOBODY(self) -> None:
        # The live catalog has `Tensor` under `Enginnering`, which no employee
        # belongs to. Failing closed is right for a precision gate - and the
        # typo is reported rather than absorbed, so it gets fixed once.
        assert untracked_work(self.owned("Enginnering"), NOTHING_TRACKED, "Engineering") == []

    def test_a_misspelt_department_is_reported(self) -> None:
        odd = mismatched_departments(self.owned("Enginnering"), ["Engineering", "Finance"])
        assert odd == ["Enginnering"]

    def test_a_department_on_the_roster_is_not_reported(self) -> None:
        assert mismatched_departments(self.owned("Engineering"), ["Engineering"]) == []

    def test_is_not_fooled_by_a_merely_similar_name(self) -> None:
        assert untracked_work(self.owned("Engineering"), NOTHING_TRACKED, "Finance") == []

    def test_a_person_with_no_department_gets_only_unowned_entities(self) -> None:
        # Fails closed: without a department there is no way to tell work from
        # tool, so only genuinely unowned projects come through.
        assert untracked_work(self.owned("Engineering"), NOTHING_TRACKED, None) == []
        assert len(untracked_work(self.owned(None), NOTHING_TRACKED, None)) == 1


class TestSpreadOverTime:
    """Three samples in one minute is ONE glance recorded three times.

    The agent samples far faster than a person changes task, so raw sighting
    counts over-count by roughly the sampling rate. Requiring the sightings to
    span separate stretches of time is what turns "it was on screen" into
    "they spent time on it" - and it is the gate that cannot be faked by a
    noisy sampler.
    """

    def clustered(self, times: int) -> list[Sighting]:
        return [Sighting(CIRCLE, "Circle", 70, None, 0) for _ in range(times)]

    def test_twenty_sightings_in_one_bucket_are_still_one_glance(self) -> None:
        assert untracked_work(self.clustered(20), NOTHING_TRACKED) == []

    def test_sightings_across_two_buckets_count(self) -> None:
        spread = [
            Sighting(CIRCLE, "Circle", 70, None, 0),
            Sighting(CIRCLE, "Circle", 70, None, 0),
            Sighting(CIRCLE, "Circle", 70, None, 1),
        ]
        assert len(untracked_work(spread, NOTHING_TRACKED)) == 1

    def test_a_caller_that_supplies_no_bucket_gets_no_free_spread(self) -> None:
        # None is one bucket, not "unknown so allow it". A caller unable to
        # say when it saw something has not demonstrated spread.
        no_buckets = [Sighting(CIRCLE, "Circle", 70) for _ in range(9)]
        assert untracked_work(no_buckets, NOTHING_TRACKED) == []

    def test_spread_alone_is_not_enough_without_the_count(self) -> None:
        # Two buckets but only two sightings, under MIN_SIGHTINGS of 3.
        thin = [Sighting(CIRCLE, "Circle", 70, None, 0), Sighting(CIRCLE, "Circle", 70, None, 1)]
        assert untracked_work(thin, NOTHING_TRACKED) == []
