"""Running a playbook: what gets created, who holds it, and what stops it.

This CREATES work for real people, so the tests that matter most are the ones
about restraint: an automatic run happens exactly once, a step nobody can be
assigned is reported rather than dumped on the subject, and cancelling gives
back the slot without destroying work somebody has already done.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmployeeStatus, Role
from app.models.playbook import (
    AssigneeRule,
    Playbook,
    PlaybookRunStatus,
    PlaybookStatus,
    PlaybookStep,
    PlaybookTrigger,
)
from app.models.resignation import Resignation, ResignationStatus
from app.models.task import Task, TaskCadence, TaskPriority, TaskStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.playbook import PlaybookRepository, PlaybookRunRepository
from app.schemas.auth import CurrentUser
from app.services.playbook_service import (
    PlaybookAlreadyRun,
    PlaybookNotRunnable,
    PlaybookService,
)
from tests.conftest import _Seed

ANCHOR = date(2026, 10, 19)


def _service(db: AsyncSession) -> PlaybookService:
    return PlaybookService(
        PlaybookRepository(db),
        PlaybookRunRepository(db),
        EmployeeRepository(db),
        AuditRepository(db),
    )


async def _joiner(db: AsyncSession, **kw: object) -> Employee:
    """A joiner with the fields a playbook step can refer to.

    Defaults are merged so a test can override any of them - `is_active=False`
    for the leaver case included.
    """
    fields: dict[str, object] = {
        "hr_external_id": "hr-joiner",
        "work_email": "aditi@corp.test",
        "full_name": "Aditi Sharma",
        "role": Role.INTERN,
        "status": EmployeeStatus.ACTIVE,
        "is_active": True,
        "department": "Social Media",
        "job_title": "SM Intern",
        "hire_date": ANCHOR,
    }
    fields["hr_external_id"] = kw.pop("ext", fields["hr_external_id"])
    fields["work_email"] = kw.pop("email", fields["work_email"])
    fields.update(kw)
    person = Employee(**fields)  # type: ignore[arg-type]
    db.add(person)
    await db.flush()
    return person


async def _playbook(
    db: AsyncSession,
    *,
    trigger: PlaybookTrigger = PlaybookTrigger.MANUAL,
    status: PlaybookStatus = PlaybookStatus.PUBLISHED,
    steps: list[PlaybookStep] | None = None,
) -> Playbook:
    book = Playbook(
        name="New joiner",
        status=status,
        trigger=trigger,
        auto_assign_since=ANCHOR,
    )
    db.add(book)
    await db.flush()
    for step in steps or [
        PlaybookStep(position=0, title_template="Induction - {name}", offset_days=0),
        PlaybookStep(position=1, title_template="Welcome Kit - Lunch", offset_days=2),
    ]:
        step.playbook_id = book.id
        db.add(step)
    await db.flush()
    return book


class TestStamping:
    async def test_creates_one_task_per_step_with_the_name_filled_in(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        joiner = await _joiner(db)
        book = await _playbook(db)

        outcome = await _service(db).run_for(book, joiner, anchor=ANCHOR)

        assert outcome.created == 2
        assert outcome.skipped == []
        titles = (
            await db.scalars(select(Task.title).where(Task.playbook_run_id == outcome.run_id))
        ).all()
        assert sorted(titles) == ["Induction - Aditi Sharma", "Welcome Kit - Lunch"]

    async def test_due_dates_count_from_the_anchor_not_from_today(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # The joiner started 19 Oct; the day-2 step is due 21 Oct however long
        # afterwards somebody runs the playbook.
        joiner = await _joiner(db)
        book = await _playbook(db)

        await _service(db).run_for(book, joiner, anchor=ANCHOR)

        due = sorted(
            d.date() for d in (await db.scalars(select(Task.due_date))).all() if d is not None
        )
        assert due == [date(2026, 10, 19), date(2026, 10, 21)]

    async def test_every_task_is_one_time_not_recurring(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # A playbook step is a one-off job. Inheriting a cadence would hand the
        # joiner an induction every morning forever.
        joiner = await _joiner(db)
        await _service(db).run_for(await _playbook(db), joiner, anchor=ANCHOR)
        cadences = set((await db.scalars(select(Task.cadence))).all())
        assert cadences == {TaskCadence.ONE_TIME}

    async def test_tasks_point_back_at_the_run(self, db: AsyncSession, seed: _Seed) -> None:
        joiner = await _joiner(db)
        outcome = await _service(db).run_for(await _playbook(db), joiner, anchor=ANCHOR)
        orphans = await db.scalar(
            select(func.count()).select_from(Task).where(Task.playbook_run_id.is_(None))
        )
        assert orphans == 0, "a stamped task with no provenance cannot be explained or undone"
        assert outcome.run_id is not None


class TestAssigneeRules:
    async def test_subject_manager_resolves_per_person(self, db: AsyncSession, seed: _Seed) -> None:
        joiner = await _joiner(db, manager_id=seed.manager.id)
        book = await _playbook(
            db,
            steps=[
                PlaybookStep(
                    position=0,
                    title_template="Set up 1:1 with {first_name}",
                    assignee_rule=AssigneeRule.SUBJECT_MANAGER,
                )
            ],
        )

        outcome = await _service(db).run_for(book, joiner, anchor=ANCHOR)

        assert outcome.created == 1
        holder = await db.scalar(
            select(Task.assignee_id).where(Task.playbook_run_id == outcome.run_id)
        )
        assert holder == seed.manager.id

    async def test_an_unresolvable_step_is_reported_not_dumped_on_the_subject(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # A joiner with no manager on file. Assigning the manager's step to the
        # joiner would hand them their own reviewer's job and look deliberate.
        joiner = await _joiner(db)
        assert joiner.manager_id is None
        book = await _playbook(
            db,
            steps=[
                PlaybookStep(
                    position=0,
                    title_template="Manager check-in",
                    assignee_rule=AssigneeRule.SUBJECT_MANAGER,
                ),
                PlaybookStep(position=1, title_template="Induction - {name}"),
            ],
        )

        outcome = await _service(db).run_for(book, joiner, anchor=ANCHOR)

        assert outcome.created == 1, "the resolvable step still runs"
        assert len(outcome.skipped) == 1
        assert "subject_manager" in outcome.skipped[0]
        assert outcome.skipped != [], "a half-run playbook must not look clean"

    async def test_a_fixed_assignee_who_has_left_does_not_get_tasks(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        leaver = await _joiner(db, ext="hr-gone", email="gone@corp.test", is_active=False)
        joiner = await _joiner(db, ext="hr-new", email="new@corp.test")
        book = await _playbook(
            db,
            steps=[
                PlaybookStep(
                    position=0,
                    title_template="Send the welcome kit",
                    assignee_rule=AssigneeRule.FIXED,
                    fixed_assignee_id=leaver.id,
                )
            ],
        )

        outcome = await _service(db).run_for(book, joiner, anchor=ANCHOR)

        assert outcome.created == 0
        assert len(outcome.skipped) == 1


class TestIdempotency:
    async def test_an_automatic_run_happens_exactly_once(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        joiner = await _joiner(db)
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        service = _service(db)

        first = await service.run_for(book, joiner, anchor=ANCHOR, automatic=True)
        assert first.created == 2

        with pytest.raises(PlaybookAlreadyRun):
            await service.run_for(book, joiner, anchor=ANCHOR, automatic=True)

        total = await db.scalar(select(func.count()).select_from(Task))
        assert total == 2, "a second sweep must not double the checklist"

    async def test_a_manual_run_is_deliberately_repeatable(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # HR clicking twice is HR's call - it is not the sweep, and refusing
        # would block legitimately re-running a corrected checklist.
        joiner = await _joiner(db)
        book = await _playbook(db)
        service = _service(db)

        await service.run_for(book, joiner, anchor=ANCHOR)
        await service.run_for(book, joiner, anchor=ANCHOR)

        assert await db.scalar(select(func.count()).select_from(Task)) == 4

    async def test_a_draft_playbook_cannot_run(self, db: AsyncSession, seed: _Seed) -> None:
        joiner = await _joiner(db)
        book = await _playbook(db, status=PlaybookStatus.DRAFT)
        with pytest.raises(PlaybookNotRunnable):
            await _service(db).run_for(book, joiner, anchor=ANCHOR)
        assert await db.scalar(select(func.count()).select_from(Task)) == 0


class TestCancelling:
    async def test_removes_untouched_tasks_and_frees_the_slot(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        joiner = await _joiner(db)
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        service = _service(db)
        outcome = await service.run_for(book, joiner, anchor=ANCHOR, automatic=True)
        run = await PlaybookRunRepository(db).get(outcome.run_id)  # type: ignore[arg-type]
        assert run is not None

        removed = await service.cancel(run, actor="hr")

        assert removed == 2
        assert run.status is PlaybookRunStatus.CANCELLED
        # The slot is free again - the thing the forms constraint made impossible.
        again = await service.run_for(book, joiner, anchor=ANCHOR, automatic=True)
        assert again.created == 2

    async def test_keeps_a_task_somebody_has_already_worked_on(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        joiner = await _joiner(db)
        service = _service(db)
        outcome = await service.run_for(await _playbook(db), joiner, anchor=ANCHOR)
        started = await db.scalar(
            select(Task).where(Task.playbook_run_id == outcome.run_id).limit(1)
        )
        assert started is not None
        started.status = TaskStatus.IN_PROGRESS
        started.completion_pct = 40
        await db.flush()

        run = await PlaybookRunRepository(db).get(outcome.run_id)  # type: ignore[arg-type]
        assert run is not None
        removed = await service.cancel(run, actor="hr")

        assert removed == 1, "only the untouched one goes"
        survivors = (await db.scalars(select(Task.title))).all()
        assert len(survivors) == 1, "real work must survive losing its provenance"


class TestValidation:
    def test_rejects_a_placeholder_nothing_can_fill(self) -> None:
        problems = PlaybookService.validation_errors(
            Playbook(name="x", trigger=PlaybookTrigger.MANUAL),
            [PlaybookStep(position=0, title_template="Induction - {nmae}")],
        )
        assert any("{nmae}" in p for p in problems)

    def test_rejects_a_fixed_step_with_nobody_chosen(self) -> None:
        problems = PlaybookService.validation_errors(
            Playbook(name="x", trigger=PlaybookTrigger.MANUAL),
            [
                PlaybookStep(
                    position=0,
                    title_template="Send the kit",
                    assignee_rule=AssigneeRule.FIXED,
                    fixed_assignee_id=None,
                )
            ],
        )
        assert any("named person" in p for p in problems)

    def test_rejects_an_empty_playbook(self) -> None:
        assert (
            PlaybookService.validation_errors(
                Playbook(name="x", trigger=PlaybookTrigger.MANUAL), []
            )
            != []
        )

    def test_accepts_a_good_one(self) -> None:
        assert (
            PlaybookService.validation_errors(
                Playbook(name="x", trigger=PlaybookTrigger.MANUAL),
                [
                    PlaybookStep(
                        position=0,
                        title_template="Induction - {name}",
                        priority=TaskPriority.HIGH,
                    )
                ],
            )
            == []
        )


class TestNewJoinerSweep:
    """The automatic half. Everything here is about restraint: an hourly tick
    must onboard each joiner once, and publishing must not onboard the people
    who were already here."""

    async def test_runs_for_a_joiner_who_started_after_publishing(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = ANCHOR
        joiner = await _joiner(db)  # hire_date == ANCHOR
        await db.flush()

        outcome = await _service(db).sweep_new_joiners(ANCHOR)

        assert outcome.runs == 1
        assert outcome.tasks == 2
        assert joiner.id is not None

    async def test_does_NOT_touch_people_who_were_already_here(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # THE backfill guard. The seeded org joined before the playbook
        # existed; publishing one must not onboard all of them.
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = ANCHOR
        old_hand = await _joiner(
            db, ext="hr-old", email="old@corp.test", hire_date=date(2025, 1, 6)
        )
        await db.flush()

        outcome = await _service(db).sweep_new_joiners(ANCHOR)

        assert outcome.runs == 0, f"onboarded somebody who joined {old_hand.hire_date}"
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_running_the_sweep_repeatedly_creates_nothing_extra(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # A worker ticks hourly forever. This is the property that makes that safe.
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = ANCHOR
        await _joiner(db)
        await db.flush()
        service = _service(db)

        first = await service.sweep_new_joiners(ANCHOR)
        second = await service.sweep_new_joiners(ANCHOR)
        third = await service.sweep_new_joiners(ANCHOR)

        assert first.runs == 1
        assert (second.runs, third.runs) == (0, 0)
        assert (second.already_done, third.already_done) == (1, 1)
        assert await db.scalar(select(func.count()).select_from(Task)) == 2

    async def test_waits_for_the_trigger_offset(self, db: AsyncSession, seed: _Seed) -> None:
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = ANCHOR
        book.trigger_days = 3
        await _joiner(db)
        await db.flush()
        service = _service(db)

        early = await service.sweep_new_joiners(ANCHOR)
        on_time = await service.sweep_new_joiners(date(2026, 10, 22))

        assert early.runs == 0, "day-3 must not fire on day 0"
        assert on_time.runs == 1

    async def test_a_joiner_with_no_start_date_is_never_guessed_at(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = ANCHOR
        await _joiner(db, ext="hr-nodate", email="nodate@corp.test", hire_date=None)
        await db.flush()

        outcome = await _service(db).sweep_new_joiners(ANCHOR)

        assert outcome.runs == 0, "no start date means no telling a joiner from old staff"

    async def test_no_live_playbook_means_the_sweep_does_nothing(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # Draft only - the sweep must be a no-op, not pick it up anyway.
        await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER, status=PlaybookStatus.DRAFT)
        await _joiner(db)
        await db.flush()

        outcome = await _service(db).sweep_new_joiners(ANCHOR)

        assert outcome.runs == 0
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_unarmed_playbook_does_nothing(self, db: AsyncSession, seed: _Seed) -> None:
        # Published but auto_assign_since never set: fire for nobody rather
        # than for everybody. The safe direction to fail in.
        book = await _playbook(db, trigger=PlaybookTrigger.NEW_JOINER)
        book.auto_assign_since = None
        await _joiner(db)
        await db.flush()

        assert (await _service(db).sweep_new_joiners(ANCHOR)).runs == 0


class TestMonthlySweep:
    """A monthly checklist fires once a MONTH, forever.

    This is the shape `(trigger, subject)` uniqueness could not express: keyed
    once-ever, November's close would have blocked December's. The period key
    is what makes both true at the same time - once a month, never twice.
    """

    async def _monthly(self, db: AsyncSession, owner_id: object, *, days: int = 0) -> Playbook:
        book = Playbook(
            name="Month-end close",
            status=PlaybookStatus.PUBLISHED,
            trigger=PlaybookTrigger.MONTH_END,
            trigger_days=days,
            owner_id=owner_id,  # type: ignore[arg-type]
        )
        db.add(book)
        await db.flush()
        for position, title in enumerate(["GSTR 1 working", "GSTR 3B working", "tds working"]):
            db.add(PlaybookStep(playbook_id=book.id, position=position, title_template=title))
        await db.flush()
        return book

    async def test_runs_on_the_last_day_of_the_month(self, db: AsyncSession, seed: _Seed) -> None:
        await self._monthly(db, seed.report.id)

        outcome = await _service(db).sweep_monthly(date(2026, 11, 30))

        assert outcome.runs == 1
        assert outcome.tasks == 3

    async def test_does_nothing_on_any_other_day(self, db: AsyncSession, seed: _Seed) -> None:
        await self._monthly(db, seed.report.id)
        service = _service(db)

        for day in (date(2026, 11, 1), date(2026, 11, 15), date(2026, 11, 29)):
            assert (await service.sweep_monthly(day)).runs == 0, f"fired on {day}"

    async def test_repeated_ticks_on_the_same_day_run_once(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # Hourly ticks: 24 of them on the 30th must produce one close.
        await self._monthly(db, seed.report.id)
        service = _service(db)

        first = await service.sweep_monthly(date(2026, 11, 30))
        second = await service.sweep_monthly(date(2026, 11, 30))

        assert (first.runs, second.runs) == (1, 0)
        assert second.already_done == 1
        assert await db.scalar(select(func.count()).select_from(Task)) == 3

    async def test_NEXT_month_runs_again(self, db: AsyncSession, seed: _Seed) -> None:
        # THE point of the period key. Once-ever uniqueness would have made
        # the December close impossible.
        await self._monthly(db, seed.report.id)
        service = _service(db)

        await service.sweep_monthly(date(2026, 11, 30))
        december = await service.sweep_monthly(date(2026, 12, 31))

        assert december.runs == 1, "the close must be able to happen again"
        assert await db.scalar(select(func.count()).select_from(Task)) == 6

    async def test_handles_a_short_february(self, db: AsyncSession, seed: _Seed) -> None:
        await self._monthly(db, seed.report.id)
        service = _service(db)

        assert (await service.sweep_monthly(date(2026, 2, 28))).runs == 1
        assert (await service.sweep_monthly(date(2026, 2, 27))).runs == 0

    async def test_can_start_early_but_steps_still_count_from_month_end(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        await self._monthly(db, seed.report.id, days=3)
        service = _service(db)

        assert (await service.sweep_monthly(date(2026, 11, 30))).runs == 0
        assert (await service.sweep_monthly(date(2026, 11, 27))).runs == 1
        due = {d.date() for d in (await db.scalars(select(Task.due_date))).all() if d}
        assert due == {date(2026, 11, 30)}, "offsets count from month end, not from the fire day"

    async def test_an_owner_who_has_left_is_reported_not_guessed_at(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        gone = await _joiner(db, ext="hr-gone-owner", email="goneowner@corp.test", is_active=False)
        await self._monthly(db, gone.id)

        outcome = await _service(db).sweep_monthly(date(2026, 11, 30))

        assert outcome.runs == 0
        assert any("owner inactive" in line for line in outcome.skipped)
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_publishing_a_monthly_playbook_without_an_owner_is_refused(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # Caught at publish, because a sweep that silently does nothing is how
        # `cadence` stayed meaningless for years.
        book = Playbook(
            name="Ownerless close",
            status=PlaybookStatus.DRAFT,
            trigger=PlaybookTrigger.MONTH_END,
        )
        db.add(book)
        await db.flush()
        db.add(PlaybookStep(playbook_id=book.id, position=0, title_template="Close the books"))
        await db.flush()
        caller = CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)

        with pytest.raises(PlaybookNotRunnable, match="owner"):
            await _service(db).publish_for(caller, book.id)


class TestOffboardingSweep:
    """The mirror of onboarding, anchored on the last working day.

    It fires BEFORE that day, because an exit checklist is only useful while
    the person is still there to hand things over.
    """

    async def _offboarding(
        self, db: AsyncSession, *, days: int = 0, armed: date = date(2026, 1, 1)
    ) -> Playbook:
        """A published offboarding playbook, armed - which is what publishing
        does. Unarmed it fires for nobody, which is its own test below."""
        book = Playbook(
            name="Leaver",
            status=PlaybookStatus.PUBLISHED,
            trigger=PlaybookTrigger.OFFBOARDING,
            trigger_days=days,
            auto_assign_since=armed,
        )
        db.add(book)
        await db.flush()
        for position, title in enumerate(["Collect the laptop", "Revoke access for {name}"]):
            db.add(PlaybookStep(playbook_id=book.id, position=position, title_template=title))
        await db.flush()
        return book

    async def _resignation(
        self, db: AsyncSession, person: Employee, last_day: date, status: ResignationStatus
    ) -> Resignation:
        row = Resignation(employee_id=person.id, last_working_day=last_day, status=status)
        db.add(row)
        await db.flush()
        return row

    async def test_runs_for_an_accepted_resignation(self, db: AsyncSession, seed: _Seed) -> None:
        leaver = await _joiner(db, ext="hr-leaver", email="leaver@corp.test")
        await self._resignation(db, leaver, date(2026, 11, 30), ResignationStatus.ACCEPTED)
        await self._offboarding(db)

        outcome = await _service(db).sweep_offboarding(date(2026, 11, 30))

        assert outcome.runs == 1
        assert outcome.tasks == 2
        titles = (await db.scalars(select(Task.title))).all()
        assert "Revoke access for Aditi Sharma" in titles

    async def test_ignores_a_resignation_nobody_has_decided_on(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # An exit checklist raised off an undecided resignation would be
        # visible to the leaver's manager before the decision is made.
        leaver = await _joiner(db, ext="hr-maybe", email="maybe@corp.test")
        await self._resignation(db, leaver, date(2026, 11, 30), ResignationStatus.SUBMITTED)
        await self._offboarding(db)

        outcome = await _service(db).sweep_offboarding(date(2026, 11, 30))

        assert outcome.runs == 0
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_fires_early_when_asked_to(self, db: AsyncSession, seed: _Seed) -> None:
        leaver = await _joiner(db, ext="hr-early", email="early@corp.test")
        await self._resignation(db, leaver, date(2026, 11, 30), ResignationStatus.ACCEPTED)
        await self._offboarding(db, days=7)
        service = _service(db)

        assert (await service.sweep_offboarding(date(2026, 11, 22))).runs == 0
        assert (await service.sweep_offboarding(date(2026, 11, 23))).runs == 1

    async def test_offboards_each_leaver_once(self, db: AsyncSession, seed: _Seed) -> None:
        leaver = await _joiner(db, ext="hr-once", email="once@corp.test")
        await self._resignation(db, leaver, date(2026, 11, 30), ResignationStatus.ACCEPTED)
        await self._offboarding(db)
        service = _service(db)

        first = await service.sweep_offboarding(date(2026, 11, 30))
        second = await service.sweep_offboarding(date(2026, 12, 1))

        assert (first.runs, second.runs) == (1, 0)
        assert second.already_done == 1
        assert await db.scalar(select(func.count()).select_from(Task)) == 2

    async def test_does_NOT_reach_back_over_everybody_who_ever_left(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        """THE backfill guard, for leavers.

        Publishing an offboarding playbook must not raise an exit checklist
        for every resignation in company history - the same accident the
        joiner sweep is guarded against, and the same shape as the 117 tasks
        a naive recurrence switch-on would have created.
        """
        old_leaver = await _joiner(db, ext="hr-gone-2024", email="gone2024@corp.test")
        await self._resignation(db, old_leaver, date(2025, 2, 28), ResignationStatus.ACCEPTED)
        # Armed in November, as publishing today would.
        await self._offboarding(db, armed=date(2026, 11, 1))

        outcome = await _service(db).sweep_offboarding(date(2026, 11, 30))

        assert outcome.runs == 0, "raised an exit checklist for somebody who left in 2025"
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_unarmed_offboarding_playbook_fires_for_nobody(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # Fails in the safe direction: no arming date means nobody, not the
        # entire back catalogue of leavers.
        leaver = await _joiner(db, ext="hr-unarmed", email="unarmed@corp.test")
        await self._resignation(db, leaver, date(2026, 11, 30), ResignationStatus.ACCEPTED)
        book = await self._offboarding(db)
        book.auto_assign_since = None
        await db.flush()

        assert (await _service(db).sweep_offboarding(date(2026, 11, 30))).runs == 0
