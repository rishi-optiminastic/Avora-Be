"""Remove demo/seeded monitoring data so the dashboards show ONLY real agent data.

Deletes every activity sample (the real agent refills its own within a minute),
drops seed-created devices (label "<name>'s laptop"), and resets the remaining
(real) devices' counters so they read offline until the agent next posts.

    uv run python scripts/purge_demo_activity.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, update

from app.db.session import SessionFactory, engine
from app.models.activity import ActivitySample
from app.models.device import Device


async def purge() -> str:
    async with SessionFactory() as session:
        samples = await session.execute(delete(ActivitySample))
        seeded = await session.execute(delete(Device).where(Device.label.like("%'s laptop")))
        await session.execute(update(Device).values(last_seen_at=None, last_sequence=0))
        await session.commit()
        return (
            f"deleted {samples.rowcount} activity samples, "
            f"{seeded.rowcount} seeded devices; reset remaining devices"
        )


async def _main() -> None:
    message = await purge()
    await engine.dispose()
    print(message)  # CLI output is intentional


if __name__ == "__main__":
    asyncio.run(_main())
