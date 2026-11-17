"""Raising tasks for untracked work, end to end against the database.

This is the only feature in the codebase where a machine decides a task
should exist with no human in the loop, so the tests that matter are the ones
about restraint: it must say nothing when the catalog is empty, nothing when
the work is already tracked, nothing twice, and never more than the cap.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivitySample
from app.models.device import Device
from app.models.employee import Employee
from app.models.screenshot import OcrStatus, Screenshot
from app.models.task import Task, TaskStatus
from app.models.work_entity import WorkEntity
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.services.untracked_work_service import PER_PERSON_CAP, UntrackedWorkService
from tests.conftest import _Seed

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def _service(db: AsyncSession) -> UntrackedWorkService:
    return UntrackedWorkService(
        ActivityRepository(db),
        ScreenshotRepository(db),
        WorkEntityRepository(db),
        TaskRepository(db),
        EmployeeRepository(db),
        AuditRepository(db),
    )


async def _entity(db: AsyncSession, name: str, keywords: list[str]) -> WorkEntity:
    row = WorkEntity(name=name, keywords=keywords, domains=[], is_active=True)
    db.add(row)
    await db.flush()
    return row


_SEQ = itertools.count(1)


async def _seen(
    db: AsyncSession,
    person: Employee,
    device: Device,
    text: str,
    times: int,
    spread_minutes: int = 45,
) -> None:
    """`times` activity samples naming the same work, SPREAD over time.

    Activity rather than screenshots, because that is the detector's primary
    signal and the only one that can satisfy the spread gate: a window title
    carries a timestamp, so it can show two separate sittings on something.
    OCR samples all share one bucket by design and can only corroborate.

    45 minutes apart by default - wider than the 30-minute bucket, so each
    sample lands in its own stretch of time, which is what real work looks
    like. A test wanting clustered samples passes a smaller number.
    """
    for i in range(times):
        when = NOW - timedelta(minutes=spread_minutes * (i + 1))
        db.add(
            ActivitySample(
                device_id=device.id,
                employee_id=person.id,
                sequence=next(_SEQ),
                client_timestamp=when,
                received_at=when,
                active_window=text,
            )
        )
    await db.flush()


async def _shots(db: AsyncSession, person: Employee, device: Device, text: str, times: int) -> None:
    """OCR samples only. Kept deliberately: it is what proves OCR alone
    cannot satisfy the spread gate."""
    for i in range(times):
        db.add(
            Screenshot(
                device_id=device.id,
                employee_id=person.id,
                captured_at=NOW - timedelta(minutes=5 * (i + 1)),
                received_at=NOW - timedelta(minutes=5 * (i + 1)),
                ocr_status=OcrStatus.DONE,
                ocr_text=text,
            )
        )
    await db.flush()


class TestRaising:
    async def test_raises_one_task_for_sustained_untracked_work(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "signalor signal-dash build", 4)

        outcome = await _service(db).sweep(NOW)

        assert outcome.created == 1
        task = await db.scalar(select(Task).where(Task.auto_detected.is_(True)))
        assert task is not None
        assert task.title == "Untracked work on Signalor"
        assert task.assignee_id == seed.report.id
        assert task.status is TaskStatus.TODO

    async def test_the_title_never_contains_screen_text(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # The whole reason this is allowed to run unattended. A task must not
        # arrive named after whatever happened to be on the screen.
        await _entity(db, "Circle", ["circle", "hrms"])
        await _seen(db, seed.report, seed.device, "circle hrms - Re: invoice from vendor Gmail", 4)

        await _service(db).sweep(NOW)

        titles = (await db.scalars(select(Task.title))).all()
        assert titles == ["Untracked work on Circle"]
        assert all("invoice" not in t.lower() and "gmail" not in t.lower() for t in titles)

    async def test_points_the_task_at_the_entity(self, db: AsyncSession, seed: _Seed) -> None:
        entity = await _entity(db, "Avora", ["avora", "pms"])
        await _seen(db, seed.report, seed.device, "avora pms screen", 4)

        await _service(db).sweep(NOW)

        linked = await db.scalar(select(Task.project_id).where(Task.auto_detected.is_(True)))
        assert linked == entity.id, "without this the dedupe cannot work"


class TestRestraint:
    async def test_says_nothing_when_the_catalog_is_empty(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # The most likely reason this feature appears to do nothing, so it
        # reports rather than returning a silent zero.
        await _seen(db, seed.report, seed.device, "some work on something", 9)

        outcome = await _service(db).sweep(NOW)

        assert outcome.created == 0
        assert any("no active work entities" in n for n in outcome.notes)

    async def test_says_nothing_about_a_glance(self, db: AsyncSession, seed: _Seed) -> None:
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "signalor", 1)

        assert (await _service(db).sweep(NOW)).created == 0

    async def test_says_nothing_when_the_work_is_already_tracked(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        entity = await _entity(db, "Signalor", ["signalor", "signal-dash"])
        db.add(
            Task(
                title="Ship the dashboard",
                assignee_id=seed.report.id,
                project_id=entity.id,
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await _seen(db, seed.report, seed.device, "signalor signal-dash", 9)
        await db.flush()

        outcome = await _service(db).sweep(NOW)

        assert outcome.created == 0, "raised a duplicate for work already on the board"

    async def test_a_second_sweep_adds_nothing(self, db: AsyncSession, seed: _Seed) -> None:
        # The created task is its own dedupe: it is open and points at the
        # entity, so the next sweep sees it as tracked.
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "signalor signal-dash", 5)
        service = _service(db)

        first = await service.sweep(NOW)
        second = await service.sweep(NOW + timedelta(hours=1))

        assert (first.created, second.created) == (1, 0)
        assert await db.scalar(select(func.count()).select_from(Task)) == 1

    async def test_completing_it_does_not_earn_another_tomorrow(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # The cooldown. Without it, a person who finishes the detected task
        # gets a fresh one every single day.
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "signalor signal-dash", 5)
        service = _service(db)
        await service.sweep(NOW)
        raised = await db.scalar(select(Task).where(Task.auto_detected.is_(True)))
        assert raised is not None
        raised.status = TaskStatus.DONE
        await db.flush()

        again = await service.sweep(NOW + timedelta(days=1))

        assert again.created == 0
        assert await db.scalar(select(func.count()).select_from(Task)) == 1

    async def test_never_more_than_the_cap_per_person(self, db: AsyncSession, seed: _Seed) -> None:
        # If the catalog is wrong, the blast radius is the cap - not one task
        # per project the person's day happened to touch.
        # SEPARATE samples per project, which is what a real multi-project day
        # looks like. Four entities named in ONE screenshot is a tie, and the
        # matcher correctly calls that ambiguous rather than picking.
        for name, extra in (
            ("Signalor", "signal-dash"),
            ("Avora", "pms"),
            ("Circle", "hrms"),
            ("Tensor", "tensorflow"),
        ):
            await _entity(db, name, [name.lower(), extra])
            await _seen(db, seed.report, seed.device, f"{name.lower()} {extra}", 4)

        outcome = await _service(db).sweep(NOW)

        assert outcome.created == PER_PERSON_CAP
        assert outcome.held_back > 0, "the cap's cost has to be visible, not silent"

    async def test_reports_how_many_people_it_looked_at(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # A sweep that considered people and raised nothing is working. It has
        # to be distinguishable from one that never ran.
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "unrelated spreadsheet work", 5)

        outcome = await _service(db).sweep(NOW)

        assert outcome.considered == 1
        assert outcome.created == 0

    async def test_two_projects_on_one_screen_creates_nothing(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        """THE accuracy guarantee, and it is worth stating out loud.

        4,903 of the 13,927 matching screenshots on the live database - 35% -
        name two entities at once. The matcher de-rates a close runner-up by
        20, which puts it under the confidence floor, so every one of those
        is thrown away rather than guessed at. A coin-flip between two
        projects is worse than silence: it is silence plus a wrong task.
        """
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _entity(db, "Circle", ["circle", "hrms"])
        # Both named equally, in every sample. No amount of repetition should
        # turn a tie into a decision.
        await _seen(db, seed.report, seed.device, "signalor signal-dash circle hrms", 20)

        outcome = await _service(db).sweep(NOW)

        assert outcome.created == 0, "picked a side in a tie"
        assert await db.scalar(select(func.count()).select_from(Task)) == 0

    async def test_names_entities_that_can_never_match(self, db: AsyncSession, seed: _Seed) -> None:
        # Three of the eight live entities fail this, `O2` with ZERO terms
        # because "o2" is under the matcher's 3-character floor. An entity
        # nobody can name looks exactly like one nobody works on.
        await _entity(db, "Recruitment", [])
        await _entity(db, "Signalor", ["signalor", "signal-dash"])
        await _seen(db, seed.report, seed.device, "signalor signal-dash", 4)

        outcome = await _service(db).sweep(NOW)

        assert any("Recruitment" in note for note in outcome.notes)
        assert any("second keyword" in note for note in outcome.notes)
        assert outcome.created == 1, "the matchable one still works"
