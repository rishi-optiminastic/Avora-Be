"""A duplicate device id must not take down a whole punch upload.

The connector sends a batch and the service resolves every punch in it, so one
unresolvable id used to 500 the entire request: a full day of office punches
vanished with nothing on screen to say why. It happened in production because an
employee who rejoined had an old offboarded record carrying the same enrollment
id as their new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeStatus, Role
from app.repositories.employee import EmployeeRepository


async def _person(
    db: AsyncSession, *, name: str, external: str, active: bool, biometric_id: str
) -> Employee:
    person = Employee(
        hr_external_id=external,
        work_email=f"{external}@acme.com",
        full_name=name,
        role=Role.EMPLOYEE,
        status=EmployeeStatus.ACTIVE if active else EmployeeStatus.INACTIVE,
        is_active=active,
        biometric_id=biometric_id,
    )
    db.add(person)
    await db.commit()
    return person


async def test_a_rejoiner_resolves_to_their_ACTIVE_record(db: AsyncSession) -> None:
    """The exact production shape: one name, two rows, one device id."""
    old = await _person(
        db, name="Ranjan Ayare", external="bio-old", active=False, biometric_id="152"
    )
    new = await _person(
        db, name="Ranjan Ayare", external="bio-new", active=True, biometric_id="152"
    )

    found = await EmployeeRepository(db).get_by_biometric_id("152")
    assert found is not None, "a duplicate id must resolve, not raise"
    assert found.id == new.id, "punches must land on the employee who still works here"
    assert found.id != old.id


async def test_two_inactive_records_resolve_to_the_most_recent(db: AsyncSession) -> None:
    """Nothing active to prefer, so the answer must still be stable rather than
    whichever row the planner happened to return first."""
    older = await _person(db, name="Gone Twice", external="bio-a", active=False, biometric_id="777")
    older.created_at = datetime.now(UTC) - timedelta(days=400)
    newer = await _person(db, name="Gone Twice", external="bio-b", active=False, biometric_id="777")
    newer.created_at = datetime.now(UTC) - timedelta(days=10)
    await db.commit()

    repo = EmployeeRepository(db)
    first = await repo.get_by_biometric_id("777")
    second = await repo.get_by_biometric_id("777")
    assert first is not None and first.id == newer.id
    assert second is not None and second.id == first.id, "the answer must be deterministic"


async def test_an_unknown_id_is_still_just_a_miss(db: AsyncSession) -> None:
    assert await EmployeeRepository(db).get_by_biometric_id("no-such-device") is None


async def test_a_single_match_is_unaffected(db: AsyncSession) -> None:
    only = await _person(
        db, name="Solo Punch", external="bio-solo", active=True, biometric_id="900"
    )
    found = await EmployeeRepository(db).get_by_biometric_id("900")
    assert found is not None and found.id == only.id
