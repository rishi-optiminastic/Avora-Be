"""EOD-settings data access — a single org-wide row (singleton).

The first read materialises the row, seeded from the env defaults so an existing
deployment keeps its current schedule/toggle the moment this ships (no behaviour
change until someone edits it in Settings).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.eod_settings import EodSettings


class EodSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> EodSettings | None:
        rows = await self._session.execute(
            select(EodSettings).order_by(EodSettings.created_at.asc()).limit(1)
        )
        return rows.scalar_one_or_none()

    async def create_default(self, settings: Settings) -> EodSettings:
        """Seed from env so the live schedule is preserved on first read.

        Guards against a stale/inverted env (e.g. a bare EOD_REPORT_HOUR=18 with the
        default 18:00 send → draft *after* send, no review window): if the seed would
        invert, fall back to the model's column defaults (16:30 draft → 18:00 send).
        Either way it's editable in Settings afterwards."""
        draft = settings.eod_report_hour * 60 + settings.eod_report_minute
        send = settings.eod_send_hour * 60 + settings.eod_send_minute
        if send <= draft:
            row = EodSettings(enabled=settings.eod_enabled)  # sane model defaults
        else:
            row = EodSettings(
                enabled=settings.eod_enabled,
                draft_hour=settings.eod_report_hour,
                draft_minute=settings.eod_report_minute,
                send_hour=settings.eod_send_hour,
                send_minute=settings.eod_send_minute,
            )
        self._session.add(row)
        await self._session.flush()
        return row

    async def flush(self) -> None:
        await self._session.flush()
