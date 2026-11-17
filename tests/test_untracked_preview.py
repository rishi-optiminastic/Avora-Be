"""The detector's preview: what it would raise, and who may ask.

The preview exists so somebody can sign off before the sweep is switched on.
That makes ONE property load-bearing above all others: the preview must agree
with what the sweep actually does. A preview that disagrees is worse than no
preview, because somebody approves it.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.activity import ActivitySample
from app.models.device import Device
from app.models.employee import Employee
from app.models.task import Task
from app.models.work_entity import WorkEntity
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.screenshot import ScreenshotRepository
from app.repositories.task import TaskRepository
from app.repositories.work_entity import WorkEntityRepository
from app.services.untracked_work_service import UntrackedWorkService
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/untracked/preview"
NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
_SEQ = itertools.count(1)


def _service(db: AsyncSession) -> UntrackedWorkService:
    return UntrackedWorkService(
        ActivityRepository(db),
        ScreenshotRepository(db),
        WorkEntityRepository(db),
        TaskRepository(db),
        EmployeeRepository(db),
        AuditRepository(db),
    )


async def _work(db: AsyncSession, person: Employee, device: Device, text: str, times: int) -> None:
    for i in range(times):
        when = NOW - timedelta(minutes=45 * (i + 1))
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


async def _setup(db: AsyncSession, seed: _Seed) -> None:
    db.add(WorkEntity(name="Signalor", keywords=["signalor", "signal-dash"], domains=[]))
    await db.flush()
    await _work(db, seed.report, seed.device, "signalor signal-dash", 4)


class TestAgreement:
    async def test_preview_writes_nothing(self, db: AsyncSession, seed: _Seed) -> None:
        await _setup(db, seed)

        outcome = await _service(db).preview(NOW)

        assert outcome.created == 1, "it still reports what it WOULD do"
        assert await db.scalar(select(func.count()).select_from(Task)) == 0, "but wrote a task"

    async def test_preview_and_sweep_decide_the_same_thing(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        """THE property. One decision path, with the write as the only branch."""
        await _setup(db, seed)
        service = _service(db)

        previewed = await service.preview(NOW)
        swept = await service.sweep(NOW)

        assert previewed.created == swept.created
        assert [f.title for f in previewed.found] == [f.title for f in swept.found]
        assert [f.entity_id for f in previewed.found] == [f.entity_id for f in swept.found]
        assert [f.sightings for f in previewed.found] == [f.sightings for f in swept.found]

    async def test_a_preview_showing_nothing_matches_a_sweep_doing_nothing(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # No catalog: both must report the same reason, not one silence and
        # one explanation.
        await _work(db, seed.report, seed.device, "some work", 4)
        service = _service(db)

        previewed = await service.preview(NOW)
        swept = await service.sweep(NOW)

        assert (previewed.created, swept.created) == (0, 0)
        assert previewed.notes == swept.notes


class TestEvidence:
    async def test_carries_the_evidence_not_just_the_verdict(
        self, db: AsyncSession, seed: _Seed
    ) -> None:
        # Somebody deciding whether to trust this needs to see WHY it fired.
        await _setup(db, seed)

        found = (await _service(db).preview(NOW)).found

        assert len(found) == 1
        assert found[0].entity_name == "Signalor"
        assert found[0].employee_name == seed.report.full_name
        assert found[0].sightings >= 3
        assert found[0].confidence >= 45
        assert found[0].title == "Untracked work on Signalor"

    async def test_reports_a_catalog_that_cannot_match(self, db: AsyncSession, seed: _Seed) -> None:
        # The actionable half: an entity with one term can never fire, and
        # looks exactly like an entity nobody works on.
        db.add(WorkEntity(name="Recruitment", keywords=[], domains=[]))
        await _setup(db, seed)

        notes = (await _service(db).preview(NOW)).notes

        assert any("Recruitment" in n for n in notes)


class TestWhoMayAsk:
    async def test_requires_authentication(self, client: AsyncClient, seed: _Seed) -> None:
        assert (await client.get(URL)).status_code == 401

    async def test_only_hr_and_admins(
        self, client: AsyncClient, settings: Settings, seed: _Seed
    ) -> None:
        # It reports what EVERYBODY is working on - closer to the attendance
        # board than to a task list - so a manager must not have it.
        for actor in (seed.report, seed.manager, seed.outsider):
            resp = await client.get(URL, headers=auth_headers(settings, actor))
            assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"

    async def test_hr_gets_it_and_it_says_whether_the_sweep_is_live(
        self, client: AsyncClient, settings: Settings, seed: _Seed
    ) -> None:
        resp = await client.get(URL, headers=auth_headers(settings, seed.admin))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # A preview showing findings while this is false means nobody is
        # getting those tasks - the reviewer has to be able to tell.
        assert body["enabled"] is False
        assert "would_create" in body and "notes" in body

    async def test_the_endpoint_never_writes(
        self, client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
    ) -> None:
        await _setup(db, seed)

        resp = await client.get(URL, headers=auth_headers(settings, seed.admin))

        assert resp.status_code == 200, resp.text
        assert await db.scalar(select(func.count()).select_from(Task)) == 0
