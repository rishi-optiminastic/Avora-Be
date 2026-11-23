"""Turning a stream of window titles into what somebody actually did.

The matcher counts hits and throws the order away; this is the part that
keeps it. So the tests are about the two ways a timeline lies: fusing
separate work into one stretch, and inventing a stretch out of a glance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.work_timeline import (
    GAP_MINUTES,
    MAX_SUBJECTS,
    MIN_STRETCH_MINUTES,
    Sample,
    Stretch,
    assemble,
    narrate,
    split_title,
)

BASE = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
MIDDOT = chr(0x00B7)


def at(minute: float, window: str | None = None, **kw: str | None) -> Sample:
    return Sample(when=BASE + timedelta(minutes=minute), window=window, **kw)


def editor(minute: float, document: str) -> Sample:
    return at(minute, window=f"{document} - be - Visual Studio Code")


class TestSplittingATitle:
    def test_the_application_comes_last(self) -> None:
        """Window titles put the document first and the app last, so the two
        ends of the title answer different questions."""
        assert split_title("payroll_export.py - be - Visual Studio Code") == (
            "Visual Studio Code",
            "payroll_export.py",
        )

    def test_a_middot_separates_too(self) -> None:
        """Browsers use a middot, and the agent captures it verbatim."""
        assert split_title(f"Tasks {MIDDOT} Avora") == ("Avora", "Tasks")

    def test_a_bare_name_is_an_application_with_nothing_open(self) -> None:
        assert split_title("Slack") == ("Slack", "")

    def test_empty_text_names_nothing(self) -> None:
        assert split_title("   ") == ("", "")


class TestOneStretch:
    def test_consecutive_samples_on_one_app_are_one_stretch(self) -> None:
        found = assemble([editor(m, "payroll.py") for m in range(4)])
        assert len(found) == 1
        assert found[0].label == "Visual Studio Code"
        assert found[0].minutes == 3
        assert found[0].samples == 4

    def test_documents_are_kept_in_the_order_they_appeared(self) -> None:
        """The sequence is the evidence - editor, then tests, then the PR."""
        found = assemble(
            [editor(0, "payroll.py"), editor(1, "payroll.py"), editor(2, "test_payroll.py")]
        )
        assert found[0].subjects == ("payroll.py", "test_payroll.py")

    def test_a_long_day_in_one_editor_names_only_the_first_few_files(self) -> None:
        found = assemble([editor(m, f"file_{m}.py") for m in range(MAX_SUBJECTS + 4)])
        assert len(found[0].subjects) == MAX_SUBJECTS


class TestWhatSplitsAStretch:
    def test_changing_application_starts_a_new_stretch(self) -> None:
        found = assemble(
            [editor(m, "payroll.py") for m in range(3)]
            + [at(m, window="Inbox - Slack") for m in (3, 4, 5)]
        )
        assert [s.label for s in found] == ["Visual Studio Code", "Slack"]

    def test_a_long_silence_splits_the_same_application(self) -> None:
        """Five minutes without a sample is a break or a stopped agent, not
        work continuing - so it must not be billed as one long stretch."""
        found = assemble(
            [editor(m, "payroll.py") for m in range(3)]
            + [editor(m, "payroll.py") for m in (3 + GAP_MINUTES + 1, 3 + GAP_MINUTES + 3)]
        )
        assert len(found) == 2

    def test_a_silence_inside_the_gap_does_not_split(self) -> None:
        found = assemble([editor(0, "payroll.py"), editor(GAP_MINUTES, "payroll.py")])
        assert len(found) == 1


class TestRestraint:
    def test_a_single_sample_is_not_a_stretch(self) -> None:
        """One sample evidences no elapsed time at all."""
        assert assemble([editor(0, "payroll.py")]) == []

    def test_a_glance_shorter_than_the_floor_is_dropped(self) -> None:
        found = assemble([editor(0, "payroll.py"), editor(MIN_STRETCH_MINUTES - 1, "payroll.py")])
        assert found == []

    def test_a_sample_naming_nothing_is_ignored(self) -> None:
        assert assemble([at(0), at(1), at(2)]) == []


class TestBrowsing:
    def test_tabs_group_by_site_not_by_browser(self) -> None:
        """Every tab reports the same application, so grouping on the window
        title would fuse two hours of unrelated work into one Chrome stretch."""
        found = assemble(
            [
                at(m, window="Google Chrome", page_title="Board", domain="figma.com")
                for m in range(3)
            ]
            + [
                at(m, window="Google Chrome", page_title="Campaigns", domain="adsmanager.com")
                for m in (3, 4, 5)
            ]
        )
        assert [s.label for s in found] == ["figma.com", "adsmanager.com"]

    def test_the_site_suffix_is_trimmed_off_a_page_title(self) -> None:
        """The label already says where this was, so repeating it in every
        document name only crowds out the part that differs."""
        found = assemble(
            [
                at(
                    m,
                    window="Google Chrome",
                    page_title=f"{page} {MIDDOT} Avora",
                    domain="avora.optiminastic.com",
                )
                for m, page in ((0, "Tasks"), (1, "Tasks"), (2, "Dashboard"))
            ]
        )
        assert found[0].subjects == ("Tasks", "Dashboard")

    def test_a_page_title_that_is_not_a_site_suffix_survives(self) -> None:
        found = assemble(
            [
                at(m, window="Chrome", page_title="Invoice - October", domain="books.zoho.com")
                for m in range(3)
            ]
        )
        assert found[0].subjects == ("Invoice - October",)


class TestOrderAndTimezones:
    def test_newest_first_input_still_makes_one_stretch(self) -> None:
        """`work_signals` returns newest-first and `samples_for_employee`
        oldest-first; handing over the wrong one must not silently produce
        hundreds of one-sample stretches."""
        samples = [editor(m, "payroll.py") for m in range(4)]
        assert assemble(list(reversed(samples))) == assemble(samples)

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        """SQLite hands back naive datetimes where Postgres hands back aware
        ones. Everything is stored UTC, so reading naive as UTC is correct
        rather than a guess - and mixing the two raises."""
        naive = [
            Sample(when=datetime(2026, 11, 20, 9, m), window="payroll.py - Code") for m in range(4)
        ]
        found = assemble(naive)
        assert len(found) == 1
        assert found[0].start.tzinfo is not None
        assert found[0].minutes == 3


class TestNarrating:
    STRETCHES = (
        Stretch("Visual Studio Code", ("payroll.py",), BASE, BASE + timedelta(minutes=96), 40),
        Stretch(
            "github.com", ("PR 412",), BASE + timedelta(hours=2), BASE + timedelta(hours=3), 30
        ),
    )

    def test_it_reads_forwards_with_durations(self) -> None:
        text = narrate(self.STRETCHES)
        assert text.splitlines() == [
            "09:00-10:36 (1h 36m) Visual Studio Code: payroll.py",
            "11:00-12:00 (1h) github.com: PR 412",
        ]

    def test_a_whole_hour_drops_the_minutes(self) -> None:
        assert "(1h)" in narrate(self.STRETCHES[1:])

    def test_the_budget_is_honoured_and_says_what_it_cut(self) -> None:
        """One thrashing day must not crowd everything else out of a prompt."""
        text = narrate(self.STRETCHES, char_budget=60)
        assert text.splitlines() == [
            "09:00-10:36 (1h 36m) Visual Studio Code: payroll.py",
            "... and 1 more stretch",
        ]

    def test_a_stretch_with_no_document_still_reports_its_time(self) -> None:
        bare = Stretch("Slack", (), BASE, BASE + timedelta(minutes=12), 10)
        assert narrate([bare]) == "09:00-09:12 (12m) Slack"

    def test_nothing_observed_narrates_to_nothing(self) -> None:
        assert narrate([]) == ""
