"""Work-session data access — open-session lookups + per-day spans."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work_session import WorkSession


def _aware(dt: datetime) -> datetime:
    """Coerce a possibly-naive stored timestamp to UTC for safe comparison
    (SQLite drops tz; Postgres keeps it)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class _SpanAcc:
    """Mutable accumulator while folding a day's sessions into one span."""

    first_in: datetime
    in_source: str | None
    ip: str | None = None
    last_out: datetime | None = None
    out_source: str | None = None
    open: bool = False


@dataclass(frozen=True)
class DaySpan:
    """A day's clock-in/out summary for one employee."""

    login_at: datetime
    logout_at: datetime | None  # None ⇒ still open (no clock-out that day)
    ip_address: str | None
    in_source: str | None = None  # source of the earliest clock-in
    out_source: str | None = None  # source of the latest clock-out (None while open)


class WorkSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_open(self, employee_id: uuid.UUID) -> WorkSession | None:
        """The employee's currently-open session (not yet clocked out), if any."""
        rows = await self._session.execute(
            select(WorkSession)
            .where(
                WorkSession.employee_id == employee_id,
                WorkSession.clock_out_at.is_(None),
            )
            .order_by(WorkSession.clock_in_at.desc())
            .limit(1)
        )
        return rows.scalar_one_or_none()

    # Sources that mean a *deliberate* end-of-day checkout (vs a biometric out-punch
    # that may just be a lunch break). Drives "stop monitoring after checkout".
    _DELIBERATE_OUT = ("dashboard", "auto")

    async def has_deliberate_checkout_since(self, employee_id: uuid.UUID, since: datetime) -> bool:
        """Did the employee deliberately clock out (dashboard / auto) on/after
        `since`? Used to suppress monitoring ingest after an end-of-day checkout."""
        row = await self._session.execute(
            select(WorkSession.id)
            .where(
                WorkSession.employee_id == employee_id,
                WorkSession.clock_out_source.in_(self._DELIBERATE_OUT),
                WorkSession.clock_out_at >= since,
            )
            .limit(1)
        )
        return row.first() is not None

    async def create(
        self,
        *,
        employee_id: uuid.UUID,
        clock_in_at: datetime,
        source: str,
        ip_address: str | None = None,
    ) -> WorkSession:
        ws = WorkSession(
            employee_id=employee_id,
            clock_in_at=clock_in_at,
            source=source,
            ip_address=ip_address,
        )
        self._session.add(ws)
        await self._session.flush()
        return ws

    async def upsert_biometric_day(
        self,
        *,
        employee_id: uuid.UUID,
        day_start: datetime,
        day_end: datetime,
        clock_in_at: datetime,
        clock_out_at: datetime | None,
        last_at: datetime,
    ) -> tuple[WorkSession, bool]:
        """Create or merge the ONE biometric session for an employee on a local
        day (`source="biometric"`). Keeps the earliest clock-in. `clock_out_at` is
        the computed clock-out (None ⇒ the latest punch is an "in", day still open).
        `last_at` is the latest punch time, used to RE-OPEN a previously-closed day
        when a newer in-punch arrives (lunch return, or correcting a stale close).
        Returns (session, created). Re-pushing the same punches is idempotent."""
        existing = await self._session.scalar(
            select(WorkSession)
            .where(
                WorkSession.employee_id == employee_id,
                WorkSession.source == "biometric",
                WorkSession.clock_in_at >= day_start,
                WorkSession.clock_in_at < day_end,
            )
            .order_by(WorkSession.clock_in_at.asc())
            .limit(1)
        )
        if existing is None:
            ws = WorkSession(
                employee_id=employee_id,
                clock_in_at=clock_in_at,
                clock_out_at=clock_out_at,
                source="biometric",
                clock_out_source="biometric" if clock_out_at is not None else None,
            )
            self._session.add(ws)
            await self._session.flush()
            return ws, True

        if clock_in_at < _aware(existing.clock_in_at):
            existing.clock_in_at = clock_in_at
        existing_out = _aware(existing.clock_out_at) if existing.clock_out_at else None
        if clock_out_at is not None:
            # An out-punch — extend (or set) the clock-out to the latest one.
            if existing_out is None or clock_out_at > existing_out:
                existing.clock_out_at = clock_out_at
                existing.clock_out_source = "biometric"
        elif existing_out is not None and _aware(last_at) >= existing_out:
            # Latest punch is an "in" at/after the recorded out → person is back in
            # (or the prior close was stale): re-open so the timer resumes.
            existing.clock_out_at = None
            existing.clock_out_source = None
        await self._session.flush()
        return existing, False

    async def list_open_before(self, cutoff: datetime) -> list[WorkSession]:
        """Every still-open session (no clock-out) that began on/before `cutoff`.
        The auto-checkout worker uses this to close forgotten sessions."""
        rows = await self._session.execute(
            select(WorkSession)
            .where(WorkSession.clock_out_at.is_(None), WorkSession.clock_in_at <= cutoff)
            .order_by(WorkSession.clock_in_at.asc())
        )
        return list(rows.scalars().all())

    async def flush(self) -> None:
        await self._session.flush()

    async def sessions_in_range(
        self,
        employee_ids: Sequence[uuid.UUID],
        start: datetime,
        end: datetime,
        *,
        source: str | None = None,
    ) -> list[tuple[uuid.UUID, datetime, datetime | None, str, str | None, str | None]]:
        """Raw (employee_id, clock_in_at, clock_out_at, source, clock_out_source,
        ip_address) over a window, for the range/monthly report to group by local
        day in Python. Pass `source` to restrict to one origin (e.g. "biometric")."""
        if not employee_ids:
            return []
        stmt = (
            select(
                WorkSession.employee_id,
                WorkSession.clock_in_at,
                WorkSession.clock_out_at,
                WorkSession.source,
                WorkSession.clock_out_source,
                WorkSession.ip_address,
            )
            .where(
                WorkSession.employee_id.in_(employee_ids),
                WorkSession.clock_in_at >= start,
                WorkSession.clock_in_at < end,
            )
            .order_by(WorkSession.clock_in_at.asc())
        )
        if source is not None:
            stmt = stmt.where(WorkSession.source == source)
        rows = await self._session.execute(stmt)
        return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows.all()]

    async def day_spans(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, DaySpan]:
        """Per-employee first clock-in / last clock-out within the day. The
        clock-out is None when any session that day is still open."""
        return await self._spans(employee_ids, start, end, source=None)

    async def biometric_day_spans(
        self, employee_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, DaySpan]:
        """First-in / last-out from BIOMETRIC sessions only (for reconciliation
        against the agent's activity)."""
        return await self._spans(employee_ids, start, end, source="biometric")

    async def _spans(
        self,
        employee_ids: Sequence[uuid.UUID],
        start: datetime,
        end: datetime,
        *,
        source: str | None,
    ) -> dict[uuid.UUID, DaySpan]:
        # Grouped in Python (not a SQL aggregate) so we can keep the SOURCE of the
        # earliest clock-in and the latest clock-out — an aggregate can't tell us
        # which row the min/max came from portably. Org-scale data, so cheap.
        rows = await self.sessions_in_range(employee_ids, start, end, source=source)
        acc: dict[uuid.UUID, _SpanAcc] = {}
        for emp_id, cin, cout, src, cout_src, ip in rows:
            a = acc.get(emp_id)
            if a is None:
                a = _SpanAcc(first_in=cin, in_source=src, ip=ip)
                acc[emp_id] = a
            if cin < a.first_in:
                a.first_in, a.in_source = cin, src
            if a.ip is None and ip is not None:
                a.ip = ip
            if cout is None:
                a.open = True
            elif a.last_out is None or cout > a.last_out:
                a.last_out, a.out_source = cout, cout_src
        return {
            emp_id: DaySpan(
                login_at=a.first_in,
                logout_at=None if a.open else a.last_out,
                ip_address=a.ip,
                in_source=a.in_source,
                out_source=None if a.open else a.out_source,
            )
            for emp_id, a in acc.items()
        }
