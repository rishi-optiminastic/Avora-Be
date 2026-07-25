"""One-off: send ONLY today's EOD drafts, leaving any earlier-day backlog as
drafts. Manual admin utility for when the auto-send window was missed (e.g. an
email-transport outage) and you want to flush just the current day without the
scheduler sweeping older leftover drafts too.

Runs the same service wiring as `worker/eod_scheduler.py`. "Today" is the local
date in the attendance/EOD timezone, matching the scheduler's own gate.

    python -m worker.send_eod_today
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.db.session import SessionFactory, engine
from worker.eod_scheduler import _build_service

log = logging.getLogger("send_eod_today")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        service = _build_service(session)
        local_dt = await service._local_dt(now)
        report_date = local_dt.date().isoformat()
        try:
            sent = await service.send_drafts_for_date(report_date, now)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    log.info("sent %d EOD report(s) for %s", sent, report_date)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
