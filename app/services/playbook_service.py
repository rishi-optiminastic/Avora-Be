"""Running a playbook: one checklist, stamped out for one person as tasks.

The business rules live here; the arithmetic and the rendering live in
`app/core/playbook.py`, pure and tested without a database.

**What this does NOT do, deliberately.** It does not decide whether a task
should exist, what it should say, or who should hold it. A human wrote the
step and chose the trigger; this resolves a name, a date and an assignee and
writes the rows. The machine contributes no judgement, which is the only
reason the result can be called accurate - the same argument that kept a
model out of `app/core/recurrence.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.playbook import (
    MONTHLY_TRIGGERS,
    Subject,
    due_on,
    is_due,
    month_anchor,
    period_key,
    render,
    unknown_placeholders,
)
from app.models.employee import Employee, Role
from app.models.playbook import (
    AssigneeRule,
    Playbook,
    PlaybookRun,
    PlaybookRunStatus,
    PlaybookStatus,
    PlaybookStep,
    PlaybookTrigger,
)
from app.models.task import Task, TaskCadence, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.playbook import PlaybookRepository, PlaybookRunRepository
from app.schemas.auth import CurrentUser
from app.schemas.playbook import PlaybookWrite

logger = get_logger("app.playbook")

SYSTEM_ACTOR = "system"


def _may_manage(caller: CurrentUser) -> bool:
    """HR and admins write playbooks and run them.

    A manager cannot: a playbook creates work for other people's reports (the
    HR lead's steps, the joiner's own), so running one reaches well outside
    any one manager's scope.
    """
    return caller.role in (Role.ADMIN, Role.HR)


@dataclass
class RunOutcome:
    """What one run did, and what it could not do.

    `skipped` is as important as `created`: a step whose assignee cannot be
    resolved - a joiner with no manager on file - must be REPORTED, not
    silently dropped. Silent skipping is how `cadence` stayed a label nothing
    read for years.
    """

    run_id: uuid.UUID | None = None
    created: int = 0
    #: One line per step that produced nothing, with the reason.
    skipped: list[str] = field(default_factory=list)


@dataclass
class SweepOutcome:
    runs: int = 0
    tasks: int = 0
    already_done: int = 0
    skipped: list[str] = field(default_factory=list)


class PlaybookAlreadyRun(ConflictError):
    """This subject has already had this automatic trigger run.

    A ConflictError, so the global handler answers 409 rather than letting it
    escape as a 500: the sweep swallows it, but HR clicking "run" on somebody
    already onboarded deserves a real answer.
    """


class PlaybookNotRunnable(ConflictError):
    """The playbook is not in a state that can be run - a draft, or no steps.

    409 for the same reason: the request is well-formed and the caller is
    allowed, the playbook is simply not ready.
    """


class PlaybookService:
    def __init__(
        self,
        playbooks: PlaybookRepository,
        runs: PlaybookRunRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._playbooks = playbooks
        self._runs = runs
        self._employees = employees
        self._audit = audit

    # --- authoring ------------------------------------------------------- #

    @staticmethod
    def validation_errors(playbook: Playbook, steps: list[PlaybookStep]) -> list[str]:
        """Why this playbook cannot be published yet, or an empty list.

        Checked at publish rather than at render, so the author is told about
        `{nmae}` while they can still fix it - instead of a joiner finding it
        in a task title. Every publish rule lives here, so there is one place
        to read what "ready" means.
        """
        problems: list[str] = []
        if not steps:
            problems.append("A playbook needs at least one step.")
        if playbook.trigger in MONTHLY_TRIGGERS and playbook.owner_id is None:
            # A monthly checklist is about a desk, so the trigger cannot
            # supply a subject the way a joining does. Caught here, because
            # discovering it from a sweep that silently does nothing is how
            # `cadence` stayed meaningless for years.
            problems.append("A monthly playbook needs an owner - pick whose checklist it is.")
        for step in steps:
            where = f"Step {step.position + 1}"
            if not step.title_template.strip():
                problems.append(f"{where} has no title.")
            for template in (step.title_template, step.description_template or ""):
                for bad in unknown_placeholders(template):
                    problems.append(f"{where} uses {{{bad}}}, which nothing can fill.")
            if step.assignee_rule is AssigneeRule.FIXED and step.fixed_assignee_id is None:
                problems.append(f"{where} is assigned to a named person, but none is chosen.")
        return problems

    # --- running --------------------------------------------------------- #

    async def run_for(
        self,
        playbook: Playbook,
        subject: Employee,
        *,
        anchor: date | None = None,
        runner_id: uuid.UUID | None = None,
        automatic: bool = False,
    ) -> RunOutcome:
        """Stamp this playbook out for this person.

        `anchor` defaults to the subject's hire date, falling back to today:
        offsets are counted from the event the checklist is ABOUT, not from
        whenever somebody got round to clicking.
        """
        if playbook.status is not PlaybookStatus.PUBLISHED:
            raise PlaybookNotRunnable(f"Playbook is {playbook.status.value}, not published.")

        trigger = playbook.trigger if automatic else PlaybookTrigger.MANUAL
        # The anchor, once: every step's offset and the period key are both
        # measured from it, and computing it twice invites them to disagree.
        on = anchor or subject.hire_date or datetime.now(UTC).date()
        period = period_key(trigger, on) if automatic else ""
        if automatic and await self._runs.already_ran(trigger, subject.id, period):
            raise PlaybookAlreadyRun(str(subject.id))

        steps = list(await self._playbooks.steps_of(playbook.id))
        if not steps:
            raise PlaybookNotRunnable("Playbook has no steps.")

        run = PlaybookRun(
            playbook_id=playbook.id,
            subject_id=subject.id,
            trigger=trigger,
            status=PlaybookRunStatus.COMPLETED,
            anchor_date=on,
            period_key=period,
            ran_by=runner_id,
        )
        self._runs.add(run)
        await self._runs.flush()

        outcome = RunOutcome(run_id=run.id)
        facts = self._subject_facts(subject)
        for step in steps:
            await self._stamp(step, run, subject, facts, on, outcome)

        run.tasks_created = outcome.created
        await self._audit.append(
            actor=str(runner_id) if runner_id else SYSTEM_ACTOR,
            action="playbook.run",
            target=f"playbook:{playbook.id}:subject:{subject.id}:tasks:{outcome.created}",
        )
        return outcome

    async def _stamp(
        self,
        step: PlaybookStep,
        run: PlaybookRun,
        subject: Employee,
        facts: Subject,
        anchor: date,
        outcome: RunOutcome,
    ) -> None:
        assignee_id = await self._assignee_for(step, subject)
        if assignee_id is None:
            # Reported, never guessed at. Assigning an unresolvable step to
            # the subject would hand a joiner their own manager's job.
            outcome.skipped.append(
                f"Step {step.position + 1} ({step.assignee_rule.value}): nobody to assign it to"
            )
            return
        task = Task(
            title=render(step.title_template, facts)[:256],
            description=(
                render(step.description_template, facts)[:2000]
                if step.description_template
                else None
            ),
            assignee_id=assignee_id,
            assigned_by_id=run.ran_by,
            priority=step.priority,
            status=TaskStatus.TODO,
            cadence=TaskCadence.ONE_TIME,
            due_date=datetime.combine(due_on(anchor, step.offset_days), time.min, tzinfo=UTC),
            expected_output=(
                render(step.expected_output, facts)[:2000] if step.expected_output else None
            ),
            playbook_run_id=run.id,
        )
        self._runs.add_task(task)
        outcome.created += 1

    @staticmethod
    def _subject_facts(subject: Employee) -> Subject:
        return Subject(
            full_name=subject.full_name,
            department=subject.department,
            job_title=subject.job_title,
            hire_date=subject.hire_date,
        )

    async def _assignee_for(self, step: PlaybookStep, subject: Employee) -> uuid.UUID | None:
        """Who holds this step's task, resolved per run.

        A rule rather than a stored id, because "the joiner's manager" is a
        different person for each joiner. Returns None when the org chart
        cannot answer, so the caller can report it.
        """
        if step.assignee_rule is AssigneeRule.SUBJECT:
            return subject.id
        if step.assignee_rule is AssigneeRule.SUBJECT_MANAGER:
            return await self._active_or_none(subject.manager_id)
        if step.assignee_rule is AssigneeRule.SUBJECT_BUDDY:
            return await self._active_or_none(subject.buddy_id)
        return await self._active_or_none(step.fixed_assignee_id)

    async def _active_or_none(self, employee_id: uuid.UUID | None) -> uuid.UUID | None:
        """An id only if it belongs to somebody still here.

        A playbook naming an HR lead who has since left must not create tasks
        nobody holds - the same rule recurrence applies when an assignee goes.
        """
        if employee_id is None:
            return None
        person = await self._employees.get(employee_id)
        return person.id if person is not None and person.is_active else None

    # --- undoing --------------------------------------------------------- #

    async def cancel(self, run: PlaybookRun, actor: str) -> int:
        """Undo a run: delete the tasks nobody has started, keep the rest.

        Cancelling also frees the (trigger, subject) slot, so the playbook can
        be corrected and run again - the thing `uq_form_assignments_trigger_subject`
        made impossible for probation forms.
        """
        removed = 0
        for task in await self._runs.untouched_tasks_of(run.id):
            await self._runs.delete_task(task)
            removed += 1
        run.status = PlaybookRunStatus.CANCELLED
        await self._audit.append(
            actor=actor,
            action="playbook.run.cancelled",
            target=f"run:{run.id}:tasks_removed:{removed}",
        )
        return removed

    # --- the authorized facade -------------------------------------------- #
    #
    # The methods above are the MECHANISM, called by the sweep with no caller.
    # These are what the API reaches, and they are the only place a caller is
    # checked - §5.3, authorization derived from the server's record of who is
    # asking, never from the request.

    async def list_for(self, caller: CurrentUser) -> list[tuple[Playbook, int]]:
        if not _may_manage(caller):
            raise AuthorizationError()
        # Two queries, flat: the library page is the one that loads every
        # visit, and counting per playbook made it 1 + N.
        counts = await self._playbooks.step_counts()
        return [(book, counts.get(book.id, 0)) for book in await self._playbooks.list_all()]

    async def detail_for(
        self, caller: CurrentUser, playbook_id: uuid.UUID
    ) -> tuple[Playbook, list[PlaybookStep]]:
        if not _may_manage(caller):
            raise AuthorizationError()
        book = await self._playbooks.get(playbook_id)
        if book is None:
            raise NotFoundError("Playbook not found")
        return book, list(await self._playbooks.steps_of(playbook_id))

    async def run_manually(
        self,
        caller: CurrentUser,
        playbook_id: uuid.UUID,
        subject_id: uuid.UUID,
        anchor: date | None,
    ) -> RunOutcome:
        if not _may_manage(caller):
            raise AuthorizationError()
        book = await self._playbooks.get(playbook_id)
        if book is None:
            raise NotFoundError("Playbook not found")
        subject = await self._employees.get(subject_id)
        if subject is None or not subject.is_active:
            raise NotFoundError("Employee not found")
        return await self.run_for(
            book, subject, anchor=anchor, runner_id=caller.employee_id, automatic=False
        )

    async def cancel_run(self, caller: CurrentUser, run_id: uuid.UUID) -> tuple[int, int]:
        """Undo a run. Returns (removed, kept)."""
        if not _may_manage(caller):
            raise AuthorizationError()
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError("Run not found")
        before = run.tasks_created
        removed = await self.cancel(run, actor=str(caller.employee_id))
        return removed, max(0, before - removed)

    async def runs_for_subject(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> list[PlaybookRun]:
        """Which playbooks have been run for somebody.

        Readable by HR and admins, and by the person themselves: a joiner
        seeing why eleven tasks appeared on their first morning is the point,
        not a leak.
        """
        if not _may_manage(caller) and caller.employee_id != employee_id:
            raise AuthorizationError()
        return list(await self._runs.for_subject(employee_id))

    async def create_for(self, caller: CurrentUser, payload: PlaybookWrite) -> uuid.UUID:
        if not _may_manage(caller):
            raise AuthorizationError()
        book = Playbook(
            name=payload.name.strip(),
            description=(payload.description or "").strip() or None,
            status=PlaybookStatus.DRAFT,
            trigger=payload.trigger,
            trigger_days=payload.trigger_days,
            owner_id=payload.owner_id,
            created_by=caller.employee_id,
        )
        self._playbooks.add(book)
        await self._playbooks.flush()
        await self._write_steps(book.id, payload)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="playbook.created",
            target=f"playbook:{book.id}",
        )
        return book.id

    async def update_for(
        self, caller: CurrentUser, playbook_id: uuid.UUID, payload: PlaybookWrite
    ) -> None:
        """Edit a playbook. Steps are REPLACED, not merged.

        Safe to replace, unlike a published form's questions, because a step
        is a template: tasks already stamped out are rows of their own and do
        not change when the template does. A run keeps meaning what it meant.
        """
        if not _may_manage(caller):
            raise AuthorizationError()
        book = await self._playbooks.get(playbook_id)
        if book is None:
            raise NotFoundError("Playbook not found")
        book.name = payload.name.strip()
        book.description = (payload.description or "").strip() or None
        book.trigger = payload.trigger
        book.trigger_days = payload.trigger_days
        book.owner_id = payload.owner_id
        await self._playbooks.delete_steps_of(playbook_id)
        await self._playbooks.flush()
        await self._write_steps(playbook_id, payload)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="playbook.updated",
            target=f"playbook:{playbook_id}",
        )

    async def _write_steps(self, playbook_id: uuid.UUID, payload: PlaybookWrite) -> None:
        for position, step in enumerate(payload.steps):
            self._playbooks.add_step(
                PlaybookStep(
                    playbook_id=playbook_id,
                    position=position,
                    title_template=step.title_template.strip(),
                    description_template=(step.description_template or "").strip() or None,
                    assignee_rule=step.assignee_rule,
                    fixed_assignee_id=step.fixed_assignee_id,
                    offset_days=step.offset_days,
                    priority=step.priority,
                    expected_output=(step.expected_output or "").strip() or None,
                )
            )
        await self._playbooks.flush()

    async def publish_for(self, caller: CurrentUser, playbook_id: uuid.UUID) -> None:
        """Make a playbook runnable, and arm its trigger from TODAY.

        `auto_assign_since = today` is the backfill guard, and it is the whole
        reason publishing is safe: without it an automatic playbook would fire
        for everybody already on the roster the moment it went live - 29
        people times eleven steps. Set here rather than at run time because
        "live from when somebody published it" is the honest meaning.
        """
        if not _may_manage(caller):
            raise AuthorizationError()
        book = await self._playbooks.get(playbook_id)
        if book is None:
            raise NotFoundError("Playbook not found")
        steps = list(await self._playbooks.steps_of(playbook_id))
        problems = self.validation_errors(book, steps)
        if problems:
            raise PlaybookNotRunnable("; ".join(problems))
        today = datetime.now(UTC).date()
        book.status = PlaybookStatus.PUBLISHED
        book.published_at = today
        if book.trigger is not PlaybookTrigger.MANUAL and book.auto_assign_since is None:
            book.auto_assign_since = today
        await self._audit.append(
            actor=str(caller.employee_id),
            action="playbook.published",
            target=f"playbook:{playbook_id}:since:{book.auto_assign_since}",
        )

    async def archive_for(self, caller: CurrentUser, playbook_id: uuid.UUID) -> None:
        """Retire a playbook. Its runs and their tasks are untouched."""
        if not _may_manage(caller):
            raise AuthorizationError()
        book = await self._playbooks.get(playbook_id)
        if book is None:
            raise NotFoundError("Playbook not found")
        book.status = PlaybookStatus.ARCHIVED
        await self._audit.append(
            actor=str(caller.employee_id),
            action="playbook.archived",
            target=f"playbook:{playbook_id}",
        )

    # --- the automatic sweep ---------------------------------------------- #

    async def sweep_new_joiners(self, today: date) -> SweepOutcome:
        """Run the live new-joiner playbook for anybody it is due for.

        Idempotent by construction: `already_ran` mirrors the partial unique
        index exactly, so ticking hourly - or re-running after a crash, or
        after a week of downtime - onboards each joiner once and no more.

        Never retroactive: `is_due` bounds candidates by the playbook's
        `auto_assign_since`, so switching this on does not onboard the whole
        company. That guard is the difference between this and the 117 tasks a
        naive recurrence switch-on would have created.
        """
        outcome = SweepOutcome()
        book = await self._playbooks.live_for_trigger(PlaybookTrigger.NEW_JOINER)
        if book is None or book.auto_assign_since is None:
            return outcome

        for person in await self._runs.joiners_since(book.auto_assign_since):
            if not is_due(
                person.hire_date, person.is_active, book.auto_assign_since, book.trigger_days, today
            ):
                continue
            if await self._runs.already_ran(PlaybookTrigger.NEW_JOINER, person.id):
                outcome.already_done += 1
                continue
            await self._tally(outcome, book, person, anchor=None, label=str(person.id))
        return outcome

    async def sweep_offboarding(self, today: date) -> SweepOutcome:
        """Run the live offboarding playbook for leavers whose day has come.

        The mirror of the joiner sweep, anchored on the last working day and
        firing `trigger_days` BEFORE it: an exit checklist is only useful
        while the person is still there.

        Guarded by `auto_assign_since` for the same reason the joiner sweep
        is: publishing must not raise an exit checklist for every resignation
        in company history.
        """
        outcome = SweepOutcome()
        book = await self._playbooks.live_for_trigger(PlaybookTrigger.OFFBOARDING)
        if book is None or book.auto_assign_since is None:
            # Unarmed fires for NOBODY, not for everybody. The safe direction.
            return outcome
        armed = book.auto_assign_since
        lead = book.trigger_days or 0
        for resignation in await self._runs.accepted_resignations(armed):
            # Bounded in SQL above and checked again here, the same two
            # independent guards the joiner path has: removing either alone
            # must not quietly reopen the whole back catalogue.
            if resignation.last_working_day < armed:
                continue
            if resignation.last_working_day - timedelta(days=lead) > today:
                continue
            person = await self._employees.get(resignation.employee_id)
            if person is None:
                continue
            if await self._runs.already_ran(PlaybookTrigger.OFFBOARDING, person.id):
                outcome.already_done += 1
                continue
            await self._tally(
                outcome, book, person, anchor=resignation.last_working_day, label=str(person.id)
            )
        return outcome

    async def sweep_monthly(self, today: date) -> SweepOutcome:
        """Run every published monthly playbook whose day is today.

        Iterates rather than picking one, because finance and ops can each
        own a month-end checklist. Idempotent per MONTH, not once ever - the
        period key is what makes December's close a different run from
        November's.
        """
        outcome = SweepOutcome()
        monthly = [PlaybookTrigger.MONTH_START, PlaybookTrigger.MONTH_END]
        for book in await self._playbooks.published_with_trigger(monthly):
            anchor = month_anchor(book.trigger, today, book.trigger_days)
            if anchor is None or book.owner_id is None:
                continue
            owner = await self._employees.get(book.owner_id)
            if owner is None or not owner.is_active:
                # The desk's owner has left. Reported, not reassigned by
                # guesswork - somebody has to say whose checklist it is now.
                outcome.skipped.append(f"{book.id}: owner inactive")
                continue
            if await self._runs.already_ran(
                book.trigger, owner.id, period_key(book.trigger, anchor)
            ):
                outcome.already_done += 1
                continue
            await self._tally(outcome, book, owner, anchor=anchor, label=str(book.id))
        return outcome

    async def _tally(
        self,
        outcome: SweepOutcome,
        book: Playbook,
        subject: Employee,
        *,
        anchor: date | None,
        label: str,
    ) -> None:
        """Run one playbook and fold the result into a sweep's totals.

        The one place a sweep failure is turned into a reported line rather
        than an exception. Shared by all three sweeps, because "a playbook
        that cannot run is somebody's job to fix, not a silence" is one rule,
        not three copies of one.
        """
        try:
            run = await self.run_for(book, subject, anchor=anchor, automatic=True)
        except (PlaybookAlreadyRun, PlaybookNotRunnable) as exc:
            outcome.skipped.append(f"{label}: {type(exc).__name__}")
            return
        outcome.runs += 1
        outcome.tasks += run.created
        outcome.skipped.extend(run.skipped)
