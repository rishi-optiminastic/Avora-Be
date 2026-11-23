"""What somebody actually did, in order, from the windows they had open.

WHY THIS EXISTS
---------------
`work_signals.attribute` scores each sample on its own and `untracked` counts
the hits, so between them the ORDER is thrown away. Ordering is the part that
carries meaning. A run of `payroll_export.py`, then `pytest`, then
`payroll_export.py`, then a pull request is nearly a transcript of an
afternoon; the same four samples as a bag of words name a project at best.

Window titles are also the widest signal there is. Measured over 30 days,
213,581 samples carry an `active_window` (99%) and 140,207 a domain (65%),
against OCR text that names a known entity in 45% of screenshots. Unlike OCR
a window title is what the person had in FRONT of them rather than everything
their screen happened to show, which is the difference between "they were
working on this" and "this was mentioned in a sidebar".

WHAT THIS IS NOT
----------------
Evidence, never a task title. `untracked.title_for` builds titles from the
entity name a human typed into the catalog, precisely so no task can arrive
called "Gmail - Re: invoice from vendor", and nothing here relaxes that rule.
A stretch is context for a person or a model to READ. Downstream code must
not copy a label into anything presented to somebody as their own work.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

#: Separators a window title puts between a document and its application:
#: hyphen, pipe, middot, en dash, em dash. Built with `chr` so this file
#: stays pure ASCII - a literal middot or dash in source trips ruff's
#: ambiguous-character check, and the agent captures all five for real.
_SEPARATORS = ("-", "|", chr(0x00B7), chr(0x2013), chr(0x2014))
_SEP_RE = re.compile(r"\s+[" + "".join(_SEPARATORS) + r"]\s+")

#: A hole longer than this ends a stretch even when the application does not
#: change. The agent samples roughly every 30 seconds, so five minutes of
#: silence is a break, a meeting, or a stopped agent - not continuous work.
GAP_MINUTES = 5

#: Below this a stretch is a glance rather than work. It also drops every
#: single-sample run, whose measured duration is zero: two samples are the
#: least that can evidence any elapsed time at all.
MIN_STRETCH_MINUTES = 2

#: Distinct documents named per stretch. An afternoon in one editor can touch
#: dozens of files, and the first few already say what the work was.
MAX_SUBJECTS = 4

#: Characters one document name may contribute. A title can be an entire
#: email subject line, and a timeline is meant to be skimmed.
MAX_SUBJECT_CHARS = 60

#: Longest trailing site name worth trimming off a page title. Above this the
#: segment is far more likely to be part of the title than a site suffix.
_MAX_SITE_SUFFIX_CHARS = 25


@dataclass(frozen=True)
class Sample:
    """One activity sample, in the shape the timeline needs.

    Deliberately NOT the `(text, domain, when)` tuple `work_signals` consumes:
    that joins the window title and the page title into one string, and the
    timeline needs them apart. A browser reports the same `window` for every
    tab, so only `page_title` says what was open in it.
    """

    when: datetime
    window: str | None = None
    page_title: str | None = None
    domain: str | None = None


@dataclass(frozen=True)
class Stretch:
    """One unbroken run of work on a single application or site."""

    #: The application or site, which is what groups the run.
    label: str
    #: Documents or pages seen in it, in the order they appeared.
    subjects: tuple[str, ...]
    start: datetime
    end: datetime
    samples: int

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _aware(when: datetime) -> datetime:
    """`when` as UTC when it arrived without a timezone.

    The same trap `untracked_work_service._bucket` documents: Postgres returns
    an aware value from a `DateTime(timezone=True)` column while SQLite hands
    back a naive one, so comparing them raises under the test suite and works
    in production, or the reverse. Everything is stored UTC, so reading a
    naive value as UTC is the correct interpretation rather than a guess.
    """
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def split_title(text: str) -> tuple[str, str]:
    """A window title's (application, document).

    Titles conventionally END with the application - "payroll_export.py - be -
    Visual Studio Code" - so the last segment names the app and the first
    names what is open in it. A title with no separator is taken as the
    application with nothing open, which is exactly what a bare "Slack" or
    "Finder" is.
    """
    parts = [part.strip() for part in _SEP_RE.split(text.strip()) if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], parts[0]


def _trim_site(title: str, domain: str) -> str:
    """A page title without the trailing site name the domain already carries.

    "Tasks - Avora" under `avora.optiminastic.com` reads as "Tasks", so a
    stretch lists what was open rather than repeating where it was open four
    times over.
    """
    parts = [part.strip() for part in _SEP_RE.split(title.strip()) if part.strip()]
    if len(parts) < 2:
        return title.strip()
    tail = parts[-1]
    squashed = tail.lower().replace(" ", "")
    if len(tail) <= _MAX_SITE_SUFFIX_CHARS and squashed in domain.lower().replace(".", ""):
        return " - ".join(parts[:-1])
    return title.strip()


def _key_and_subject(sample: Sample) -> tuple[str, str]:
    """What groups this sample, and what was open at the time.

    A domain beats the window title for grouping, because every browser tab
    reports the same application: grouping on the title would fuse an hour in
    Figma and an hour in Meta Ads Manager into one "Google Chrome" stretch and
    lose the only thing worth knowing.
    """
    if sample.domain and sample.domain.strip():
        domain = sample.domain.strip().lower()
        title = (sample.page_title or "").strip()
        if title:
            return domain, _trim_site(title, domain)
        return domain, split_title(sample.window or "")[1]
    return split_title(sample.window or "")


def assemble(samples: Sequence[Sample]) -> list[Stretch]:
    """Consecutive samples grouped into runs of work on one application.

    Sorted here rather than trusted. `work_signals` returns newest-first while
    `samples_for_employee` returns oldest-first, and a caller that handed over
    the wrong one would otherwise get hundreds of one-sample stretches instead
    of an error.
    """
    ordered = sorted(
        (sample for sample in samples if _key_and_subject(sample)[0]),
        key=lambda sample: _aware(sample.when),
    )
    out: list[Stretch] = []
    run: list[Sample] = []
    key = ""
    for sample in ordered:
        sample_key = _key_and_subject(sample)[0]
        gap = _aware(sample.when) - _aware(run[-1].when) if run else timedelta()
        if run and (sample_key != key or gap > timedelta(minutes=GAP_MINUTES)):
            out.extend(_close(run, key))
            run = []
        key = sample_key
        run.append(sample)
    out.extend(_close(run, key))
    return out


def _close(run: Sequence[Sample], key: str) -> list[Stretch]:
    """A finished run as a `Stretch`, or nothing at all when it was a glance."""
    if len(run) < 2:
        return []
    start, end = _aware(run[0].when), _aware(run[-1].when)
    if end - start < timedelta(minutes=MIN_STRETCH_MINUTES):
        return []
    subjects: list[str] = []
    for sample in run:
        subject = _key_and_subject(sample)[1][:MAX_SUBJECT_CHARS].strip()
        if subject and subject not in subjects:
            subjects.append(subject)
    return [Stretch(key, tuple(subjects[:MAX_SUBJECTS]), start, end, len(run))]


def _hm(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def narrate(stretches: Sequence[Stretch], *, tz: tzinfo = UTC, char_budget: int = 2000) -> str:
    """The timeline as lines a person or a model can read.

    Forwards in time, and deliberately not sorted by length: a day reads
    forwards, and the sequence is the whole point. "Editor, then tests, then
    the pull request" is a story that the same three stretches ordered by
    duration is not.

    Budgeted the way `eod_ocr_char_budget` budgets screen text, so one
    thrashing day cannot crowd out everything else in a prompt.
    """
    lines: list[str] = []
    used = 0
    for index, stretch in enumerate(stretches):
        head = (
            f"{stretch.start.astimezone(tz):%H:%M}-{stretch.end.astimezone(tz):%H:%M} "
            f"({_hm(stretch.minutes)}) {stretch.label}"
        )
        line = f"{head}: {', '.join(stretch.subjects)}" if stretch.subjects else head
        if used + len(line) + 1 > char_budget:
            left = len(stretches) - index
            lines.append(f"... and {left} more stretch{'' if left == 1 else 'es'}")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
