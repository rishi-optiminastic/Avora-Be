"""Move biometric attendance from one employee to another over a date range.

Two people enrolled on the office terminal under each other's device id have
each other's attendance. Avora matched every punch correctly - to the id it was
told - so the punches, the times and the sessions are all right. Only the name
on them is wrong.

That makes deleting and re-ingesting the wrong repair: it throws away correct
rows to rebuild them, and `upsert_biometric_day` only ever moves a clock-in
EARLIER, so a re-ingest over existing rows merges the two people instead of
replacing them. Swapping `employee_id` keeps every recorded time exactly as the
device reported it and is the smallest change that can be right.

    uv run python -m scripts.reattribute_biometric \
        --from 2026-10-05 --to 2026-10-31 \
        --swap <email-or-id> <email-or-id>

Dry run by default: it prints the before/after day by day and writes nothing.
Add --apply to commit. The operation is its own inverse, so running it twice
returns the data to where it started - which is the undo if a range is wrong.

Scope: sessions with `source="biometric"` only. A manual clock-in or the laptop
agent's own record belongs to whoever made it and is never touched.

Workspace: this runs against the default schema (`public`), which is the first
workspace. It will not find anyone who lives in another workspace's schema, and
says so rather than doing nothing silently.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory
from app.models.employee import Employee
from app.models.work_session import WorkSession
from app.repositories.audit import AuditRepository

OFFICE_TZ = "Asia/Kolkata"


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def _resolve(session: AsyncSession, token: str) -> Employee:
    """Find an employee by work email or by id, so an operator can paste either."""
    try:
        where = Employee.id == uuid.UUID(token)
    except ValueError:
        where = Employee.work_email == token.strip().lower()
    found = await session.scalar(select(Employee).where(where))
    if found is None:
        sys.exit(f"no employee matches {token!r}")
    return found


def _bounds(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The local day range as UTC instants: [start 00:00, end+1 00:00)."""
    begin = datetime(start.year, start.month, start.day, tzinfo=tz)
    stop = datetime(end.year, end.month, end.day, tzinfo=tz) + timedelta(days=1)
    return begin.astimezone(UTC), stop.astimezone(UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _local_day(moment: datetime, tz: ZoneInfo) -> str:
    return _aware(moment).astimezone(tz).date().isoformat()


def _clock(moment: datetime | None, tz: ZoneInfo) -> str:
    return "--:--" if moment is None else _aware(moment).astimezone(tz).strftime("%H:%M")


def _report(rows: list[WorkSession], a: Employee, b: Employee, tz: ZoneInfo) -> None:
    """Day by day, who holds each session now and who will hold it after."""
    by_day: dict[str, dict[uuid.UUID, WorkSession]] = {}
    for row in rows:
        by_day.setdefault(_local_day(row.clock_in_at, tz), {})[row.employee_id] = row

    width = max(len(a.full_name), len(b.full_name), 12)
    print(f"\n{'day':<12}{a.full_name + ' (now)':<{width + 20}}{b.full_name + ' (now)'}")
    print(f"{'':12}{'-> becomes ' + b.full_name:<{width + 20}}{'-> becomes ' + a.full_name}")
    print("-" * (12 + 2 * (width + 20)))
    for day in sorted(by_day):
        held = by_day[day]
        cells: list[str] = []
        for employee in (a, b):
            owned = held.get(employee.id)
            cells.append(
                f"{_clock(owned.clock_in_at, tz)} -> {_clock(owned.clock_out_at, tz)}"
                if owned
                else "-"
            )
        print(f"{day:<12}{cells[0]:<{width + 20}}{cells[1]}")
    print()


async def load_biometric_sessions(
    session: AsyncSession, ids: list[uuid.UUID], begin: datetime, stop: datetime
) -> list[WorkSession]:
    """Both people's biometric sessions whose clock-in falls in the window.

    Only `source="biometric"`: a manual clock-in or the agent's own record was
    made by the person it names and must not move.
    """
    result = await session.execute(
        select(WorkSession).where(
            WorkSession.employee_id.in_(ids),
            WorkSession.source == "biometric",
            WorkSession.clock_in_at >= begin,
            WorkSession.clock_in_at < stop,
        )
    )
    return list(result.scalars().all())


def swap_owners(rows: list[WorkSession], a_id: uuid.UUID, b_id: uuid.UUID) -> None:
    """Point each session at the other person. Self-inverse, which is the undo."""
    for row in rows:
        row.employee_id = b_id if row.employee_id == a_id else a_id


def move_owners(rows: list[WorkSession], a_id: uuid.UUID, b_id: uuid.UUID) -> None:
    """One-way: everything of A's becomes B's, and B's own rows stay put.

    Needed when the two people's days are not a clean exchange. Correcting the
    device ids mid-afternoon split one day in half: the morning punch had already
    landed on the wrong person, while the evening punch - arriving after the
    correction - opened a SECOND session on the right one. A swap would hand the
    evening back to the wrong person; only a one-way move plus a merge is right.
    """
    for row in rows:
        if row.employee_id == a_id:
            row.employee_id = b_id


def merge_same_day(rows: list[WorkSession], tz: ZoneInfo) -> list[WorkSession]:
    """Collapse one person's duplicate biometric sessions on a local day.

    The ingest keeps exactly one session per employee-day, so two can only exist
    because ownership moved underneath it. Keep the earliest clock-in, take the
    latest clock-out, and return the now-redundant rows for the caller to delete.
    """
    grouped: dict[tuple[uuid.UUID, str], list[WorkSession]] = {}
    for row in rows:
        grouped.setdefault((row.employee_id, _local_day(row.clock_in_at, tz)), []).append(row)

    redundant: list[WorkSession] = []
    for same_day in grouped.values():
        if len(same_day) < 2:
            continue
        same_day.sort(key=lambda r: _aware(r.clock_in_at))
        keeper, extras = same_day[0], same_day[1:]
        ends = [_aware(r.clock_out_at) for r in same_day if r.clock_out_at is not None]
        if ends:
            latest = max(ends)
            current = _aware(keeper.clock_out_at) if keeper.clock_out_at else None
            if current is None or latest > current:
                keeper.clock_out_at = latest
                keeper.clock_out_source = "biometric"
        redundant.extend(extras)
    return redundant


async def reattribute(
    first: str,
    second: str,
    start: date,
    end: date,
    *,
    apply: bool,
    tz_name: str,
    one_way: bool,
) -> None:
    tz = ZoneInfo(tz_name)
    begin, stop = _bounds(start, end, tz)

    async with SessionFactory() as session:
        a = await _resolve(session, first)
        b = await _resolve(session, second)
        if a.id == b.id:
            sys.exit("both arguments resolve to the same employee")

        rows = await load_biometric_sessions(session, [a.id, b.id], begin, stop)

        arrow = "->" if one_way else "<->"
        print(f"{a.full_name} {arrow} {b.full_name}" + ("  (one-way move)" if one_way else ""))
        print(f"{start} .. {end} ({tz_name}) - {len(rows)} biometric sessions in range")
        if not rows:
            print("nothing to move")
            return
        _report(rows, a, b, tz)

        if not apply:
            print("DRY RUN - nothing written. Re-run with --apply to commit.")
            return

        if one_way:
            move_owners(rows, a.id, b.id)
        else:
            swap_owners(rows, a.id, b.id)
        redundant = merge_same_day(rows, tz)
        for row in redundant:
            await session.delete(row)

        audit = AuditRepository(session)
        for owner, other in ((a, b), (b, a)):
            await audit.append(
                actor="scripts.reattribute_biometric",
                action="attendance.reattributed",
                target=f"employee:{owner.id}:from:{other.id}:{start}..{end}:{len(rows)}",
            )
        await session.commit()
        merged = f", merged away {len(redundant)} duplicate day(s)" if redundant else ""
        undo = "" if one_way or redundant else " Run the same command again to undo."
        print(f"moved {len(rows) - len(redundant)} sessions{merged}.{undo}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--swap",
        nargs=2,
        required=True,
        metavar=("EMPLOYEE", "EMPLOYEE"),
        help=(
            "the two people, by work email or employee id. Names the pair for "
            "both modes - add --move for a one-way move rather than a swap"
        ),
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help=(
            "one-way: give the FIRST --swap person's sessions to the SECOND, "
            "merging a day they both hold. Without it the two are exchanged."
        ),
    )
    parser.add_argument("--tz", default=OFFICE_TZ, help=f"office timezone (default {OFFICE_TZ})")
    parser.add_argument("--apply", action="store_true", help="commit (default is a dry run)")
    args = parser.parse_args()

    start, end = _parse_day(args.start), _parse_day(args.end)
    if end < start:
        sys.exit("--to is before --from")
    asyncio.run(
        reattribute(
            args.swap[0],
            args.swap[1],
            start,
            end,
            apply=args.apply,
            tz_name=args.tz,
            one_way=args.move,
        )
    )


if __name__ == "__main__":
    main()
