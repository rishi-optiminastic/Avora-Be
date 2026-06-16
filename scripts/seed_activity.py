"""Seed realistic activity so Attendance + Monitoring render real data (dev/demo).

Enrolls one device per active employee and inserts a day's worth of activity
samples spread across working hours (server-stamped `received_at`), so the
attendance/productivity rollups derive real login/logout/idle and ~most people
show "online now". The production Go agent posts the *same* samples via the
HMAC-signed `/activity/ingest` path — this just backfills for a live demo.

    uv run python scripts/seed_activity.py        # seed today for everyone
"""
# ruff: noqa: S311 — pseudo-random is fine here; this generates fake demo data, not secrets.

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.categories import extract_domain
from app.core.config import get_settings
from app.core.security import generate_device_token, hash_device_token
from app.db.session import SessionFactory, engine
from app.models.activity import ActivitySample
from app.models.device import Device
from app.models.employee import Employee, EmployeeStatus

# (active_window, url|None, weight) — browser rows carry a URL so the browsing
# view derives a real productive/neutral/distracting split (productive weighted
# heaviest, distracting rare).
ACTIVITIES: list[tuple[str, str | None, int]] = [
    ("Code", None, 22),
    ("Terminal", None, 10),
    ("Slack", None, 8),
    ("Figma", None, 6),
    ("Zoom", None, 4),
    ("Google Chrome", "https://github.com/optiminastic/avora", 16),
    ("Google Chrome", "https://stackoverflow.com/questions/4321", 8),
    ("Google Chrome", "https://docs.google.com/document/d/abc", 7),
    ("Google Chrome", "https://linear.app/optiminastic/board", 6),
    ("Google Chrome", "https://chatgpt.com/c/xyz", 5),
    ("Google Chrome", "https://mail.google.com/mail/u/0", 5),
    ("Google Chrome", "https://www.youtube.com/watch?v=demo", 4),
    ("Safari", "https://www.reddit.com/r/programming", 3),
]
SAMPLE_EVERY = timedelta(minutes=10)


async def _device_for(session: AsyncSession, settings: object, employee: Employee) -> Device:
    existing = (
        (await session.execute(select(Device).where(Device.employee_id == employee.id)))
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    first = employee.full_name.split(" ", 1)[0]
    device = Device(
        employee_id=employee.id,
        label=f"{first}'s laptop",
        token_hash=hash_device_token(settings, generate_device_token()),  # type: ignore[arg-type]
    )
    session.add(device)
    await session.flush()
    return device


def _samples(
    device: Device, employee: Employee, now: datetime, seq_start: int
) -> list[ActivitySample]:
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    login = day_start + timedelta(hours=9, minutes=random.randint(0, 75))
    if login > now - timedelta(minutes=20):
        login = now - timedelta(hours=2)
    online = random.random() < 0.75
    end = now if online else now - timedelta(hours=random.randint(1, 4))
    if end < login:
        end = login + timedelta(minutes=30)

    out: list[ActivitySample] = []
    stamp, seq = login, seq_start
    while stamp <= end:
        idle = random.choices([random.randint(0, 90), random.randint(300, 1200)], weights=[85, 15])[
            0
        ]
        window, url, _ = random.choices(ACTIVITIES, weights=[a[2] for a in ACTIVITIES])[0]
        out.append(
            ActivitySample(
                device_id=device.id,
                employee_id=employee.id,
                sequence=seq,
                client_timestamp=stamp,
                received_at=stamp,
                active_window=window,
                idle_seconds=idle,
                url=url,
                domain=extract_domain(url),
                flags=[],
            )
        )
        stamp += SAMPLE_EVERY
        seq += 1
    return out


async def seed_activity() -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    total_samples = 0
    employees_seeded = 0

    async with SessionFactory() as session:
        employees = (
            (
                await session.execute(
                    select(Employee).where(
                        Employee.is_active.is_(True), Employee.status == EmployeeStatus.ACTIVE
                    )
                )
            )
            .scalars()
            .all()
        )

        for employee in employees:
            device = await _device_for(session, settings, employee)
            # Idempotent: clear today's samples for this device, then re-seed.
            await session.execute(
                delete(ActivitySample).where(
                    ActivitySample.device_id == device.id,
                    ActivitySample.received_at >= day_start,
                )
            )
            samples = _samples(device, employee, now, device.last_sequence + 1)
            if not samples:
                continue
            session.add_all(samples)
            device.last_sequence = samples[-1].sequence
            device.last_seen_at = samples[-1].received_at
            total_samples += len(samples)
            employees_seeded += 1

        await session.commit()

    return f"seeded {total_samples} samples across {employees_seeded} employees for {now.date()}"


async def _main() -> None:
    message = await seed_activity()
    await engine.dispose()
    print(message)  # CLI output is intentional


if __name__ == "__main__":
    asyncio.run(_main())
