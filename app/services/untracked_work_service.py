"""Raising a task for work that is happening with no task behind it.

The autonomous half of task creation. Recurrence covers "this again, on a
cadence"; playbooks cover "this event, so these steps"; this covers the third
case - somebody is demonstrably working on a project and nothing in Avora
says so.

**Why this is allowed to act without a human, when nothing else in this file
tree is.** The machine supplies only the DETECTION. It never writes a word:
the task's title is built from the work entity's name, which a person typed
into the catalog, and `tasks.project_id` points at that entity. Nothing is
read off the screen into the task, so a task cannot arrive titled "Gmail -
Re: invoice from vendor".

**Measured before it was built.** Of 30,617 OCR'd screenshots on the live
database the catalog names 45.5%, and 35% of those matches are ambiguous
between two entities - only 29.5% are a confident single match. The gates in
`app/core/untracked.py` throw the rest away on purpose, and `PER_PERSON_CAP`
below bounds the damage of being wrong anyway.

**Its own dedupe, with no extra table.** A created task is open and points at
the entity, so the next sweep sees that entity as tracked and says nothing.
The cooldown window extends that past completion, so finishing today's task
does not earn another one tomorrow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.core.exceptions import AuthorizationError
from app.core.logging import get_logger
from app.core.untracked import (
    Candidate,
    Sighting,
    mismatched_departments,
    title_for,
    unmatchable,
    untracked_work,
)
from app.core.work_signals import EntityProfile, attribute, build_profile
from app.models.employee import Employee, Role
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.auth import CurrentUser

logger = get_logger("app.untracked")

SYSTEM_ACTOR = "system"

#: At most this many tasks per person per sweep. The single most important
#: number here: if the catalog is wrong, or somebody's day genuinely touches
#: six projects, the blast radius is two rows rather than six.
PER_PERSON_CAP = 2

#: How far back to read screen samples. A working day, so "sustained" means
#: sustained today rather than over a fortnight.
LOOKBACK_HOURS = 10

#: How wide a "stretch of time" is for the spread gate. Half an hour: short
#: enough that two buckets means genuinely separate sittings, long enough that
#: a fast sampler cannot manufacture spread out of one of them.
BUCKET_MINUTES = 30

#: Once a task has been raised for an entity, say nothing more about it for
#: this long even after it is completed. Without it, finishing today's task
#: earns another one tomorrow, forever.
COOLDOWN_DAYS = 7


@dataclass(frozen=True)
class Catalog:
    """The work-entity catalog in the two shapes a sweep needs."""

    profiles: list[EntityProfile]
    #: Who owns each entity, for the department gate.
    departments: dict[uuid.UUID, str | None]

    def named(self) -> list[tuple[uuid.UUID, str]]:
        return [(p.entity_id, p.name) for p in self.profiles]


@dataclass(frozen=True)
class SweepContext:
    """Everything one sweep needs that does not change per person.

    A value passed down rather than attributes set on the service: a sweep
    holding its own date on `self` is state that outlives the call, and two
    overlapping sweeps would quietly read each other's.
    """

    catalog: Catalog
    today: date
    #: The window start, so a sample's time bucket is numbered consistently.
    since: datetime


@dataclass(frozen=True)
class Found:
    """One thing the detector decided, in the shape a reviewer needs.

    Carries the evidence, not just the verdict: somebody deciding whether to
    trust this needs to see WHY it fired, and "6 sightings at 70% confidence"
    is checkable in a way "we think so" is not.
    """

    employee_id: uuid.UUID
    employee_name: str
    entity_id: uuid.UUID
    entity_name: str
    sightings: int
    confidence: int
    title: str


@dataclass
class DetectionOutcome:
    """What one sweep did. `considered` matters as much as `created`: a sweep
    that looked at thirty people and raised nothing is working correctly, and
    needs to be distinguishable from one that never ran."""

    considered: int = 0
    created: int = 0
    #: Candidates the per-person cap discarded, so the cap's cost is visible.
    held_back: int = 0
    titles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Every decision with its evidence, for a reviewer. Populated whether or
    #: not the tasks were written.
    found: list[Found] = field(default_factory=list)


class UntrackedWorkService:
    def __init__(
        self,
        activity: ActivityRepository,
        screenshots: ScreenshotRepository,
        entities: WorkEntityRepository,
        tasks: TaskRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._activity = activity
        self._screenshots = screenshots
        self._entities = entities
        self._tasks = tasks
        self._employees = employees
        self._audit = audit

    async def preview(self, now: datetime) -> DetectionOutcome:
        """What the sweep WOULD raise, writing nothing.

        The same decision path as `sweep`, stopping short of the insert. It
        exists because the honest validation of a detector that writes into
        people's task lists is "show me the names first, then I will switch
        it on" - and until this existed that was only possible by running a
        script against production over SSH, which is not something HR can do.
        """
        return await self._decide(now, write=False)

    async def sweep(self, now: datetime) -> DetectionOutcome:
        """Raise tasks for untracked work across everybody with screen data."""
        return await self._decide(now, write=True)

    async def _decide(self, now: datetime, *, write: bool) -> DetectionOutcome:
        """The whole decision, with the write as the single branch at the end.

        One path rather than two, so a preview cannot drift from what the
        sweep actually does - a preview that disagrees with reality is worse
        than no preview, because somebody signs off on it.
        """
        outcome = DetectionOutcome()
        catalog = await self._catalog()
        profiles = catalog.profiles
        if not profiles:
            # No catalog means nothing can be named. Said out loud rather than
            # returning a silent zero, because an empty catalog is the single
            # most likely reason this feature appears to do nothing.
            outcome.notes.append("no active work entities: nothing can be named")
            return outcome

        # An entity nobody can name looks identical to one nobody works on.
        # Named here so the fix - give it a second keyword - is obvious.
        thin = unmatchable(profiles)
        if thin:
            outcome.notes.append(
                f"{len(thin)} entities need a second keyword before they can ever match: "
                + ", ".join(thin)
            )

        people = await self._employees.list_all_active()
        by_id = {p.id: p for p in people}
        # An entity filed under a department nobody belongs to is invisible to
        # everybody. Said out loud rather than discovered months later.
        orphaned = mismatched_departments(
            [Sighting(e, n, 0, catalog.departments[e]) for e, n in catalog.named()],
            [p.department for p in people],
        )
        if orphaned:
            outcome.notes.append(
                "entity departments matching nobody on the roster: " + ", ".join(orphaned)
            )
        since = now - timedelta(hours=LOOKBACK_HOURS)
        context = SweepContext(catalog=catalog, today=now.date(), since=since)
        ids = [p.id for p in people]

        # Activity FIRST, and it is the better signal by a wide margin.
        # Measured over 30 days: 213,581 samples carry a window title (99%)
        # and 140,207 carry a domain (65%), and a domain hit alone clears the
        # matcher's confidence floor. OCR reads the whole screen including a
        # sidebar mentioning another project - which is precisely how an
        # Avora dashboard produced a confident-looking match for Circle.
        activity = await self._activity.work_signals(ids, since)
        ocr = await self._screenshots.ocr_samples(ids, since)

        for person_id in set(activity) | set(ocr):
            outcome.considered += 1
            observed = [
                (text, domain, self._bucket(when, context.since))
                for text, domain, when in activity.get(person_id, [])
            ]
            # OCR gets REAL buckets too. Giving it a fixed one looked like a
            # harmless simplification and silently disabled it: a dry run
            # against live data went from 4 tasks to zero, because the spread
            # gate can never be met by evidence that all claims the same
            # instant.
            observed += [
                (text, None, self._bucket(when, context.since))
                for text, when in ocr.get(person_id, [])
            ]
            await self._for_one(
                person_id, observed, context, by_id.get(person_id), outcome, write=write
            )
        return outcome

    @staticmethod
    def _bucket(when: datetime, since: datetime) -> int:
        """Which half-hour stretch a sample falls in, counted from the window
        start so the numbering is stable within one sweep.

        Both sides are forced tz-aware first, and that is not defensive
        clutter: Postgres returns `received_at` aware from a
        `DateTime(timezone=True)` column while SQLite hands back a naive
        value, so subtracting them raises under the test suite and works in
        production - or the reverse, which is worse. Everything is stored UTC,
        so reading a naive value as UTC is the correct interpretation rather
        than a guess. Same family of trap as the recurrence due-date bug.
        """
        left = when if when.tzinfo else when.replace(tzinfo=UTC)
        right = since if since.tzinfo else since.replace(tzinfo=UTC)
        return int((left - right).total_seconds() // (BUCKET_MINUTES * 60))

    async def _catalog(self) -> Catalog:
        """The catalog as match profiles, plus who owns each entity.

        Built once per sweep rather than per person: the profiles are the same
        for everybody, and rebuilding them 32 times was free only because the
        catalog is small today.
        """
        entities = await self._entities.list_active()
        return Catalog(
            profiles=[
                build_profile(e.id, e.name, list(e.keywords), list(e.domains)) for e in entities
            ],
            departments={e.id: e.department for e in entities},
        )

    async def _for_one(
        self,
        person_id: uuid.UUID,
        observed: list[tuple[str, str | None, int]],
        context: SweepContext,
        person: Employee | None,
        outcome: DetectionOutcome,
        *,
        write: bool = True,
    ) -> None:
        profiles = context.catalog.profiles
        departments = context.catalog.departments
        sightings = [
            Sighting(
                match.entity_id,
                match.name,
                match.confidence,
                departments.get(match.entity_id),
                bucket,
            )
            for match, bucket in (
                (attribute(text, domain, profiles), bucket) for text, domain, bucket in observed
            )
            if match is not None
        ]
        if not sightings:
            return
        tracked = await self._tasks.entities_with_recent_task(
            person_id, context.today - timedelta(days=COOLDOWN_DAYS)
        )
        # The person's own department, so an entity owned by another one is
        # read as a tool they use rather than work they do. Passed in from the
        # roster the sweep already loaded, not re-fetched per person.
        candidates = untracked_work(
            sightings, tracked, person.department if person is not None else None
        )
        if len(candidates) > PER_PERSON_CAP:
            outcome.held_back += len(candidates) - PER_PERSON_CAP
        for candidate in candidates[:PER_PERSON_CAP]:
            if write:
                await self._raise(person_id, candidate, context.today, outcome)
            else:
                outcome.created += 1
                outcome.titles.append(f"{title_for(candidate.name)} - {person_id}")
            outcome.found.append(
                Found(
                    employee_id=person_id,
                    employee_name=person.full_name if person is not None else "?",
                    entity_id=candidate.entity_id,
                    entity_name=candidate.name,
                    sightings=candidate.sightings,
                    confidence=candidate.confidence,
                    title=title_for(candidate.name),
                )
            )

    async def _raise(
        self,
        person_id: uuid.UUID,
        candidate: Candidate,
        today: date,
        outcome: DetectionOutcome,
    ) -> None:
        task = Task(
            title=title_for(candidate.name),
            # What the person can act on, and all of it verifiable: the
            # project, how many samples, how sure. No screen text.
            description=(
                f"Seen on screen {candidate.sightings} times today with no task "
                f"against {candidate.name}. Rename it, re-point it, or delete it."
            ),
            assignee_id=person_id,
            project_id=candidate.entity_id,
            priority=TaskPriority.LOW,
            status=TaskStatus.TODO,
            cadence=TaskCadence.ONE_TIME,
            due_date=datetime.combine(today, time.min, tzinfo=UTC),
            auto_detected=True,
        )
        self._tasks.add(task)
        await self._tasks.flush()
        await self._audit.append(
            actor=SYSTEM_ACTOR,
            action="task.detected",
            target=(
                f"task:{task.id}:entity:{candidate.entity_id}"
                f":sightings:{candidate.sightings}:confidence:{candidate.confidence}"
            ),
        )
        outcome.created += 1
        outcome.titles.append(task.title)

    # --- the authorized facade -------------------------------------------- #

    async def preview_for(self, caller: CurrentUser, now: datetime) -> DetectionOutcome:
        """`preview`, for a caller. HR and admins only.

        It reports what everybody is working on, which is a far wider read
        than one person's own tasks - closer to the attendance board than to
        a task list - so it sits with HR rather than with managers.
        """
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()
        return await self.preview(now)
