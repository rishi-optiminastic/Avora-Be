"""How many queries a playbook costs. Measured, then asserted, so it stays cheap.

These numbers are upper bounds, not targets. The point is that they do not
grow with the number of steps or playbooks: a run is a handful of queries
whether the checklist has 2 steps or 60, and listing is flat whatever the
library looks like. An N+1 here is quiet - it only hurts once HR has written
a dozen playbooks - so it is pinned with a test rather than left to notice.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmployeeStatus, Role
from app.models.playbook import (
    AssigneeRule,
    Playbook,
    PlaybookStatus,
    PlaybookStep,
    PlaybookTrigger,
)
from app.models.resignation import Resignation, ResignationStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.playbook import PlaybookRepository, PlaybookRunRepository
from app.schemas.auth import CurrentUser
from app.services.playbook_service import PlaybookService
from tests.conftest import _Seed

ANCHOR = date(2026, 10, 19)


@contextmanager
def counted(db: AsyncSession) -> Iterator[list[str]]:
    """Every SQL statement issued on this session's engine, in order."""
    seen: list[str] = []
    # `get_bind()` on an async session hands back the SYNC engine underneath,
    # which is the one that emits the events.
    engine = db.get_bind()

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        seen.append(statement.split("\n")[0][:60])

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _service(db: AsyncSession) -> PlaybookService:
    return PlaybookService(
        PlaybookRepository(db),
        PlaybookRunRepository(db),
        EmployeeRepository(db),
        AuditRepository(db),
    )


async def _big_playbook(db: AsyncSession, steps: int, lead_id: object) -> Playbook:
    """A realistic checklist: mostly the joiner's own, some the manager's, and
    a run of steps that all belong to the same HR desk."""
    book = Playbook(
        name="New joiner",
        status=PlaybookStatus.PUBLISHED,
        trigger=PlaybookTrigger.MANUAL,
    )
    db.add(book)
    await db.flush()
    for i in range(steps):
        rule, fixed = AssigneeRule.SUBJECT, None
        if i % 3 == 1:
            rule = AssigneeRule.SUBJECT_MANAGER
        elif i % 3 == 2:
            rule, fixed = AssigneeRule.FIXED, lead_id
        db.add(
            PlaybookStep(
                playbook_id=book.id,
                position=i,
                title_template=f"Step {i} for " + "{first_name}",
                assignee_rule=rule,
                fixed_assignee_id=fixed,  # type: ignore[arg-type]
                offset_days=i % 5,
            )
        )
    await db.flush()
    return book


async def _joiner(db: AsyncSession, manager_id: object) -> Employee:
    person = Employee(
        hr_external_id="hr-eff",
        work_email="eff@corp.test",
        full_name="Aditi Sharma",
        role=Role.INTERN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        hire_date=ANCHOR,
        manager_id=manager_id,  # type: ignore[arg-type]
    )
    db.add(person)
    await db.flush()
    return person


def _caller(seed: _Seed) -> CurrentUser:
    return CurrentUser(employee_id=seed.admin.id, role=Role.ADMIN, manager_id=None)


async def test_a_run_does_not_cost_a_query_per_step(
    db: AsyncSession, seed: _Seed, capsys: object
) -> None:
    """A 30-step run must not cost 30 lookups.

    The assignee RULES repeat - most steps go to the joiner, the rest to one
    manager and one HR desk - so the distinct people involved is what the
    cost should track, not the length of the checklist.
    """
    joiner = await _joiner(db, seed.manager.id)
    book = await _big_playbook(db, 30, seed.admin.id)
    service = _service(db)

    with counted(db) as small:
        await service.run_for(book, joiner, anchor=ANCHOR)

    # Reset and run a much longer checklist for the same shapes.
    joiner2 = Employee(
        hr_external_id="hr-eff2",
        work_email="eff2@corp.test",
        full_name="Sahil Rao",
        role=Role.INTERN,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        hire_date=ANCHOR,
        manager_id=seed.manager.id,
    )
    db.add(joiner2)
    await db.flush()
    book2 = await _big_playbook(db, 60, seed.admin.id)

    with counted(db) as big:
        await service.run_for(book2, joiner2, anchor=ANCHOR)

    # Doubling the steps must not double the queries.
    assert len(big) <= len(small) + 3, (
        f"queries grew with step count: 30 steps -> {len(small)}, 60 steps -> {len(big)}"
    )


async def test_listing_playbooks_is_flat_not_one_query_per_playbook(
    db: AsyncSession, seed: _Seed
) -> None:
    """Listing eight playbooks must not cost eight extra queries.

    This is the N+1 that matters most: the library page loads on every visit,
    and the count is all it needs - not every step row.
    """
    for i in range(8):
        book = Playbook(
            name=f"Book {i}",
            status=PlaybookStatus.PUBLISHED,
            trigger=PlaybookTrigger.MANUAL,
        )
        db.add(book)
        await db.flush()
        for position in range(10):
            db.add(
                PlaybookStep(
                    playbook_id=book.id,
                    position=position,
                    title_template="Step " + "{name}",
                )
            )
    await db.flush()
    service = _service(db)

    with counted(db) as queries:
        rows = await service.list_for(_caller(seed))

    assert len(rows) == 8
    assert [count for _, count in rows] == [10] * 8, "the counts must still be right"
    assert len(queries) <= 3, f"one query per playbook: {len(queries)} for 8 playbooks"


async def test_the_sweep_cost_grows_with_joiners_not_with_the_roster(
    db: AsyncSession, seed: _Seed
) -> None:
    """The unattended half. It ticks hourly forever, so its cost matters most.

    Two things bound it, both already in place: the query is filtered by
    `auto_assign_since` in SQL, so the whole roster is never loaded; and a
    joiner already onboarded costs ONE existence check, not a run.
    """
    book = Playbook(
        name="Joining",
        status=PlaybookStatus.PUBLISHED,
        trigger=PlaybookTrigger.NEW_JOINER,
        auto_assign_since=ANCHOR,
    )
    db.add(book)
    await db.flush()
    for position in range(10):
        db.add(
            PlaybookStep(playbook_id=book.id, position=position, title_template="Step " + "{name}")
        )
    # Twelve people who joined long BEFORE the playbook was armed. None of
    # them should cost anything at all - they must not even be fetched.
    for i in range(12):
        db.add(
            Employee(
                hr_external_id=f"hr-old-{i}",
                work_email=f"old{i}@corp.test",
                full_name=f"Old Hand {i}",
                role=Role.EXECUTIVE,
                status=EmployeeStatus.ACTIVE,
                is_active=True,
                hire_date=date(2025, 3, 1),
            )
        )
    await db.flush()
    service = _service(db)

    # Nobody due: the floor cost of a tick.
    with counted(db) as idle:
        first = await service.sweep_new_joiners(ANCHOR)
    assert first.runs == 0

    await _joiner(db, seed.manager.id)
    with counted(db) as working:
        second = await service.sweep_new_joiners(ANCHOR)
    assert second.runs == 1

    # The already-onboarded case is what runs on all 8,759 other ticks.
    with counted(db) as settled:
        third = await service.sweep_new_joiners(ANCHOR)
    assert third.runs == 0 and third.already_done == 1

    assert len(idle) <= 3, f"an idle tick cost {len(idle)} queries"
    assert len(settled) <= len(working), (
        f"a settled tick ({len(settled)}) must not cost more than a working one ({len(working)})"
    )


async def test_the_offboarding_sweep_does_not_load_every_leaver_ever(
    db: AsyncSession, seed: _Seed
) -> None:
    """The back catalogue must stay out of an hourly tick.

    This was a real bug: `accepted_resignations` had no date bound, so every
    accepted resignation in company history was loaded on every tick AND
    checked one by one - a cost that grows forever. `auto_assign_since` now
    bounds it in SQL, doing double duty as the backfill guard.
    """
    book = Playbook(
        name="Leaver",
        status=PlaybookStatus.PUBLISHED,
        trigger=PlaybookTrigger.OFFBOARDING,
        auto_assign_since=date(2026, 11, 1),
    )
    db.add(book)
    await db.flush()
    db.add(PlaybookStep(playbook_id=book.id, position=0, title_template="Collect the laptop"))

    # Thirty historical leavers, all accepted, none of them this sweep's
    # business. Each one that reached the loop would cost an existence check.
    for i in range(30):
        person = Employee(
            hr_external_id=f"hr-past-{i}",
            work_email=f"past{i}@corp.test",
            full_name=f"Past Leaver {i}",
            role=Role.EXECUTIVE,
            status=EmployeeStatus.ACTIVE,
            is_active=True,
        )
        db.add(person)
        await db.flush()
        db.add(
            Resignation(
                employee_id=person.id,
                last_working_day=date(2025, 6, 30),
                status=ResignationStatus.ACCEPTED,
            )
        )
    await db.flush()

    outcome = await _service(db).sweep_offboarding(date(2026, 11, 30))
    assert outcome.runs == 0

    # Asserted on ROWS FETCHED, not on query count - and that distinction is
    # the point. The unbounded version loaded all thirty in ONE query and
    # discarded them in Python, so counting queries saw nothing wrong while
    # the cost grew with every leaver the company ever had.
    fetched = await PlaybookRunRepository(db).accepted_resignations(date(2026, 11, 1))

    assert len(fetched) == 0, (
        f"the sweep fetched {len(fetched)} historical resignations it can never act on"
    )
