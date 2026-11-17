"""Propose work-entity keywords from the screen text you already have.

WHY THIS EXISTS
---------------
The untracked-work detector can only name work the catalog describes.
Measured on the live database: of 30,617 OCR'd screenshots the catalog names
45.5%, 35% of those matches are ambiguous, and only 29.5% are a confident
single match. Three of the eight entities cannot be matched at all - `O2` has
ZERO matchable terms, because "o2" is two characters and the matcher drops
tokens under three to avoid noise.

The catalog is the lever, and this script reads your own screen text to say
what belongs in it.

WHY NO MODEL (YET)
------------------
Frequency analysis answers most of this for nothing, and it uses the SAME
tokeniser as `app/core/work_signals.py`, so every candidate it proposes is
guaranteed to be matchable by the thing that will consume it. A model would
help GROUP terms into entities ("gstr", "gstr1", "3b" are one job), which is
the obvious next step - deliberately left out here so this costs nothing to
run and nothing to trust. Same order as the task paste importer: parse in
code first, add a model only where code genuinely cannot answer.

WHAT IT RANKS BY, AND WHY THAT TOOK TWO ATTEMPTS
------------------------------------------------
First attempt ranked by reach - distinct people times distinct days - and the
output was entirely web chrome: "whatsapp", "url", "terms", "password",
"mode", every one of them at exactly 18 people x 25 days. Ubiquitous words
SATURATE at the maximum, so ranking by reach puts the least useful terms on
top by construction.

So the signal is DISTINCTIVENESS, not reach. A term on everybody's screen
names the web; a term on a few screens, repeatedly, names their work. "gstr"
belongs to one person in Finance and that is exactly what makes it an entity.

Hence the ceiling: a term seen by more than MAX_PEOPLE_FRACTION of the roster
is dropped before ranking, however persistent it is. Within what remains,
persistence still matters, so the rank is days x people.

PRIVACY
-------
Prints TOKENS and counts only, never a line of screen text. Screen content can
hold anything, so nothing from it reaches the output.

USAGE
    DATABASE_URL=... uv run python scripts/propose_work_entities.py [--days 30] [--top 60]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.work_signals import build_profile, matchable_terms
from app.db.session import SessionFactory, engine
from app.models.screenshot import OcrStatus, Screenshot
from app.models.work_entity import WorkEntity

#: Interface chrome and generic verbs. These appear on everybody's screen all
#: day and name no work, so they would otherwise dominate the ranking.
STOPWORDS = frozenset(
    """
    the and for you are with this that from have not but all can has was were your
    file edit view help window tab new open close save print search settings options
    chrome safari firefox edge google gmail mail inbox sent draft reply forward
    com www http https html index page home login logout sign signin account profile
    add del delete remove cancel submit send done next back prev continue
    name email date time today yesterday tomorrow week month year
    total amount value number count list item items type kind status
    copy paste cut undo redo select all none more less show hide
    loading error warning success failed please try again
    sheet sheets docs doc document slide slides excel word pdf
    zoom meet teams slack notion jira figma github gitlab
    """.split()
)

#: A term nobody but one person ever sees is not a shared entity.
MIN_PEOPLE = 2
#: Nor is one seen on a single day.
MIN_DAYS = 3

#: A term on more than this share of the roster is interface, not work. The
#: number that makes this script useful rather than a list of browser chrome:
#: without it the output was "whatsapp", "url", "password", "mode".
MAX_PEOPLE_FRACTION = 0.5


async def main(days: int, top: int) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    async with SessionFactory() as session:
        entities = (await session.scalars(select(WorkEntity))).all()
        known: set[str] = set()
        for entity in entities:
            profile = build_profile(
                entity.id, entity.name, list(entity.keywords), list(entity.domains)
            )
            known |= set(profile.keyword_terms)

        rows = await session.execute(
            select(Screenshot.employee_id, Screenshot.captured_at, Screenshot.ocr_text).where(
                Screenshot.received_at >= since,
                Screenshot.ocr_status == OcrStatus.DONE,
                Screenshot.ocr_text.is_not(None),
            )
        )
        people: dict[str, set[object]] = defaultdict(set)
        dates: dict[str, set[object]] = defaultdict(set)
        shots = 0
        for employee_id, captured_at, text in rows.all():
            if not text:
                continue
            shots += 1
            day = captured_at.date() if captured_at else None
            for token in matchable_terms(text):
                # `matchable_terms` applies the matcher's own length floor, so
                # nothing proposed here is a term it could never match.
                if token in STOPWORDS or token in known:
                    continue
                if token.isdigit():
                    continue
                people[token].add(employee_id)
                dates[token].add(day)

    roster = len({p for seen in people.values() for p in seen})
    ceiling = max(MIN_PEOPLE, int(roster * MAX_PEOPLE_FRACTION))
    ranked = sorted(
        (
            (len(people[t]) * len(dates[t]), len(people[t]), len(dates[t]), t)
            for t in people
            if MIN_PEOPLE <= len(people[t]) <= ceiling and len(dates[t]) >= MIN_DAYS
        ),
        reverse=True,
    )

    print(f"Read {shots} OCR'd screenshots over {days} days.")
    print(f"Catalog already matches {len(known)} terms across {len(entities)} entities.")
    print(
        f"{roster} people seen; ignoring terms on more than {ceiling} of them "
        "(interface, not work).\n"
    )
    if not ranked:
        print("No candidates. Either the window is too short or the catalog covers everything.")
        return
    print(f"{'candidate':<28} {'people':>7} {'days':>6} {'weight':>7}")
    print("-" * 52)
    for weight, n_people, n_days, token in ranked[:top]:
        print(f"{token:<28} {n_people:>7} {n_days:>6} {weight:>7}")
    print(
        f"\n{len(ranked)} candidates in total. Group the ones that belong to the same job and\n"
        "add them as keywords on that entity - two or more terms per entity, or the\n"
        "matcher scores it below its floor and can never name it."
    )
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.top))
