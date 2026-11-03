"""Background-worker liveness.

The celebrations scheduler was killed and never restarted. No birthday,
anniversary or festival email went out for seven weeks and nothing anywhere
said so, because its heartbeat URL had never been configured - and an
unconfigured ping is indistinguishable from a healthy worker.

These tests pin the two things that make that impossible to repeat: a declared
list of workers that must be running, and a check that the declaration matches
what the deployment actually starts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.workers import EXPECTED_WORKERS
from app.repositories.worker_heartbeat import WorkerHeartbeatRepository
from app.schemas.worker_health import WorkerState
from app.services.worker_health_service import WorkerHealthService
from tests.conftest import _Seed, auth_headers

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---- the declaration must match the deployment ----------------------------- #


def test_every_declared_worker_is_actually_started_by_the_compose() -> None:
    """The guard that would have caught this.

    `EXPECTED_WORKERS` asserts a worker should be running; the compose is what
    actually runs it. When those two disagree, somebody is relying on a job that
    nothing starts - which is exactly how celebrations was lost.
    """
    compose = (REPO_ROOT / "deploy" / "docker-compose.prod.yml").read_text()
    started = set(re.findall(r'"python",\s*"-m",\s*"(worker\.[a-z_]+)"', compose))
    # The OCR worker is a separate slim image with its own entrypoint.
    started.add("worker.ocr_worker")

    missing = [s.name for s in EXPECTED_WORKERS if s.module not in started]
    assert not missing, f"declared but never started by the compose: {missing}"


def test_every_declared_worker_module_exists() -> None:
    """A module renamed without updating the list would make the compose guard
    pass against a service that cannot start."""
    for spec in EXPECTED_WORKERS:
        path = REPO_ROOT / (spec.module.replace(".", "/") + ".py")
        assert path.exists(), f"{spec.name}: no such module {spec.module}"


def test_every_declared_worker_records_its_own_name() -> None:
    """A worker that records the wrong name reports as never-seen forever, which
    is a false alarm that trains people to ignore the real one."""
    worker_dir = REPO_ROOT / "worker"
    recorded = set()
    for path in worker_dir.glob("*.py"):
        recorded.update(re.findall(r'record\("([a-z-]+)"\)', path.read_text()))
    declared = {spec.name for spec in EXPECTED_WORKERS}
    assert declared - recorded == set(), f"declared but never recorded: {declared - recorded}"
    assert recorded - declared == set(), f"recorded but not declared: {recorded - declared}"


def test_worker_names_are_unique() -> None:
    names = [spec.name for spec in EXPECTED_WORKERS]
    assert len(names) == len(set(names))


# ---- the three states ------------------------------------------------------- #


async def test_a_worker_that_has_never_run_reads_as_never_seen(db: AsyncSession) -> None:
    """The case an outward ping can never report: no row at all."""
    summary = await WorkerHealthService(WorkerHeartbeatRepository(db)).status_unscoped()
    assert summary.healthy is False
    assert {w.state for w in summary.workers} == {WorkerState.NEVER_SEEN}
    # The alert has to be able to say what is broken, not just that something is.
    celebrations = next(w for w in summary.workers if w.name == "celebrations")
    assert "Birthday" in celebrations.impact


async def test_a_recent_beat_reads_as_ok(db: AsyncSession) -> None:
    repo = WorkerHeartbeatRepository(db)
    await repo.beat("celebrations")
    await db.commit()

    summary = await WorkerHealthService(repo).status_unscoped()
    celebrations = next(w for w in summary.workers if w.name == "celebrations")
    assert celebrations.state is WorkerState.OK
    assert celebrations.minutes_since == 0
    assert summary.healthy is False  # the others still have not run


async def test_an_old_beat_reads_as_stale(db: AsyncSession) -> None:
    repo = WorkerHeartbeatRepository(db)
    spec = next(s for s in EXPECTED_WORKERS if s.name == "celebrations")
    stale_at = datetime.now(UTC) - timedelta(minutes=spec.max_silence_minutes + 5)
    await repo.beat("celebrations", now=stale_at)
    await db.commit()

    summary = await WorkerHealthService(repo).status_unscoped()
    celebrations = next(w for w in summary.workers if w.name == "celebrations")
    assert celebrations.state is WorkerState.STALE
    assert celebrations.minutes_since is not None
    assert celebrations.minutes_since > spec.max_silence_minutes


async def test_healthy_only_when_every_worker_is_ok(db: AsyncSession) -> None:
    repo = WorkerHeartbeatRepository(db)
    for spec in EXPECTED_WORKERS:
        await repo.beat(spec.name)
    await db.commit()

    summary = await WorkerHealthService(repo).status_unscoped()
    assert summary.healthy is True
    assert len(summary.workers) == len(EXPECTED_WORKERS)


async def test_beating_twice_updates_rather_than_duplicates(db: AsyncSession) -> None:
    repo = WorkerHeartbeatRepository(db)
    first = datetime.now(UTC) - timedelta(hours=2)
    await repo.beat("eod", now=first)
    await repo.beat("eod")
    await db.commit()

    rows = [r for r in await repo.all() if r.name == "eod"]
    assert len(rows) == 1
    stored = rows[0].last_beat_at
    aware = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    assert aware > first


# ---- authorization ---------------------------------------------------------- #


async def test_health_is_admin_or_hr_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """It names every job the deployment depends on - a map of the system."""
    url = "/api/v1/workers/health"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=auth_headers(settings, seed.report))).status_code == 403
    assert (await client.get(url, headers=auth_headers(settings, seed.admin))).status_code == 200


async def test_health_endpoint_lists_every_expected_worker(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    resp = await client.get("/api/v1/workers/health", headers=auth_headers(settings, seed.admin))
    body = resp.json()
    assert {w["name"] for w in body["workers"]} == {s.name for s in EXPECTED_WORKERS}
    assert body["healthy"] is False
