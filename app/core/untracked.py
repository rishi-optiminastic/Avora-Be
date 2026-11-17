"""Work happening on screen with no task behind it. Pure, so every rule is
unit-tested directly.

This is the one place a machine is allowed to decide a task should EXIST, so
the rules are about restraint rather than coverage.

**What makes it accurate, measured on the live database first.** Of 30,617
OCR'd screenshots, the work-entity catalog names 45.5% of them - and 4,903 of
those matches, 35%, are ambiguous between two entities. Only 29.5% are a
confident single match. So a detector that acted on every match would be
guessing on a third of what it touched.

Hence the three gates, each of which throws work away on purpose:

* **Sustained, not glanced at.** One screenshot mentioning a project is a tab
  somebody opened, not work. A run of them across separate samples is.
* **Confident, not ambiguous.** `work_signals.attribute` already de-rates a
  match with a close runner-up; this refuses those outright. The 4,903
  ambiguous screenshots must never create a task.
* **Untracked, not merely unmatched.** If an open task already points at the
  entity, the work IS tracked and there is nothing to add.

**What is never invented.** The task's title comes from the ENTITY NAME, which
a human typed into the catalog. Nothing is derived from the screen text, so a
task can never arrive titled "Gmail - Re: invoice from vendor". Same rule as
recurrence and playbooks: the machine supplies the trigger, a person supplied
the words.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.work_signals import EntityProfile

#: How many separate samples must name an entity before it counts as work.
#: Three, not one: a single hit is a glance, and raising this is the cheapest
#: way to trade coverage for precision if the first run proves noisy.
MIN_SIGHTINGS = 3

#: Below this, the matcher itself was unsure. `attribute` scores a clear hit
#: at 45+ and subtracts 20 when a second entity scores within 80% of the
#: best, so this floor excludes exactly the ambiguous cases.
MIN_CONFIDENCE = 45

#: Distinct stretches of time a sighting must span before it counts as work.
#: Three samples inside one minute is ONE glance recorded three times: the
#: agent samples far faster than a person changes task, so counting raw
#: sightings over-counts by roughly the sampling rate. Requiring spread is
#: what turns "it was on screen" into "they spent time on it".
MIN_BUCKETS = 2


@dataclass(frozen=True)
class Sighting:
    """One sample of screen text that named one entity."""

    entity_id: uuid.UUID
    name: str
    confidence: int
    #: The entity's owning department, or None for a cross-company project.
    #: The fourth gate - see `belongs_to`.
    department: str | None = None
    #: Which stretch of time this was seen in - the caller decides how wide a
    #: stretch is. The fifth gate: sightings must be SPREAD, not clustered.
    #: None is treated as its own bucket, so a caller that cannot supply one
    #: is not silently credited with spread it never demonstrated.
    bucket: int | None = None


@dataclass(frozen=True)
class Candidate:
    """An entity somebody is demonstrably working on, with no task for it."""

    entity_id: uuid.UUID
    name: str
    sightings: int
    #: The best confidence seen, for the audit trail and for ranking.
    confidence: int


def untracked_work(
    sightings: Sequence[Sighting],
    tracked_entity_ids: frozenset[uuid.UUID],
    viewer_department: str | None = None,
    *,
    min_sightings: int = MIN_SIGHTINGS,
) -> list[Candidate]:
    """Entities worked on enough, confidently enough, with nothing tracking it.

    Ordered by how much evidence there is, so a caller that caps the number
    it acts on takes the best-supported first.

    `min_sightings` is keyword-only and deliberately kept: it is the cheapest
    lever for trading coverage against precision if a real run proves noisy,
    and the dry run against live data showed precision is exactly where this
    feature lives or dies.
    """
    confident = [
        s for s in sightings if s.confidence >= MIN_CONFIDENCE and belongs_to(s, viewer_department)
    ]
    counts = Counter(s.entity_id for s in confident)
    # How many distinct stretches of time each entity was seen in, which is
    # what separates an afternoon's work from one glance sampled repeatedly.
    spread: dict[uuid.UUID, set[int | None]] = {}
    for sighting in confident:
        spread.setdefault(sighting.entity_id, set()).add(sighting.bucket)
    best: dict[uuid.UUID, Sighting] = {}
    for sighting in confident:
        current = best.get(sighting.entity_id)
        if current is None or sighting.confidence > current.confidence:
            best[sighting.entity_id] = sighting

    found = [
        Candidate(
            entity_id=entity_id,
            name=best[entity_id].name,
            sightings=count,
            confidence=best[entity_id].confidence,
        )
        for entity_id, count in counts.items()
        if (
            count >= min_sightings
            and len(spread.get(entity_id, set())) >= MIN_BUCKETS
            and entity_id not in tracked_entity_ids
        )
    ]
    found.sort(key=lambda c: (c.sightings, c.confidence), reverse=True)
    return found


def title_for(entity_name: str) -> str:
    """What the created task is called.

    The entity's own name and a plain verb - never a word from the screen.
    Deliberately dull: a person reading their list must be able to tell at a
    glance that nobody wrote this for them, and the `Auto` badge plus this
    phrasing both say so.
    """
    return f"Untracked work on {entity_name.strip()}"[:256]


#: The matcher scores a keyword hit at 2 and calls anything under 3 "Unknown",
#: so from text alone an entity needs at least two distinct matchable terms.
#: A domain hit is worth 3 on its own, but the detector reads OCR text and has
#: no browsing domain to offer, so for this sweep text is all there is.
MIN_TERMS_TO_MATCH = 2


def unmatchable(profiles: Sequence[EntityProfile]) -> list[str]:
    """Names of catalog entries this sweep can never match, and why it matters.

    Measured on the live catalog: three of eight entities fail this. `O2` has
    ZERO matchable terms, because "o2" is two characters and the matcher drops
    tokens under three to avoid noise - so it is reachable only by domain.
    `Recruitment` and `Tensor` have one term each, which scores 2 against a
    floor of 3.

    An entity nobody can name is indistinguishable from an entity nobody
    works on, and that confusion is exactly how `cadence` went unread for
    years. Reported so the fix - add a second keyword - is obvious.
    """
    return sorted(
        profile.name for profile in profiles if len(profile.keyword_terms) < MIN_TERMS_TO_MATCH
    )


def _same(left: str | None, right: str | None) -> bool:
    """Department equality: normalised, and deliberately EXACT after that.

    An earlier version tried to forgive the misspelling the live roster
    actually holds - it contains both `Engineering` and `Enginnering`. Two
    reasons that was wrong, and the first is the one that matters:

    1. This is a PRECISION gate. Its entire job is to stop somebody being
       handed another department's tool as a task, and near-matching makes
       that more likely, not less: "Sales" and "Scale" are two edits apart.
    2. Absorbing the typo hides it. A misspelt department on an entity is a
       data bug somebody should fix once, not something every sweep should
       quietly work around forever - so `mismatched_departments` reports it.
    """
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    return bool(a) and a == b


def mismatched_departments(sightings: Sequence[Sighting], known: Sequence[str | None]) -> list[str]:
    """Entity departments that match nothing on the roster.

    The live catalog has `Tensor` under `Enginnering`, which no employee
    belongs to, so that entity is invisible to every single person. Reported,
    because an entity nobody can be matched against looks exactly like an
    entity nobody works on - the confusion that let `cadence` go unread for
    years.
    """
    roster = {(d or "").strip().lower() for d in known if (d or "").strip()}
    odd = {
        (s.department or "").strip()
        for s in sightings
        if (s.department or "").strip() and (s.department or "").strip().lower() not in roster
    }
    return sorted(odd)


def belongs_to(sighting: Sighting, viewer_department: str | None) -> bool:
    """Is this entity plausibly this person's WORK, or just a tool they use?

    THE precision gate, and it was added because the dry run against live data
    proved it was needed. With only the first three gates the detector
    proposed, in one afternoon:

        Social Media Intern   -> "Untracked work on Avora"
        Finance Manager       -> "Untracked work on Avora"
        HR Manager            -> "Untracked work on Circle"

    All three statements were literally TRUE - the words were on screen - and
    all three were useless. Avora and Circle are products this company builds
    AND tools every employee opens daily, so a text match cannot tell "I am
    building the HR system" from "I am doing HR admin in it". Six of ten
    proposals were that mistake.

    The rule: an entity that names a department is work for THAT department;
    an entity naming none is a cross-company project and belongs to anybody.
    No new column, no model - the catalog already carries the answer.
    """
    if sighting.department is None or not sighting.department.strip():
        return True
    return _same(sighting.department, viewer_department)
