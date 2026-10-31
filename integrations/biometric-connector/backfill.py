#!/usr/bin/env python3
"""One-off backfill: push a DATE RANGE of biometric punches into Avora.

For when the office PC that normally syncs has been off and a month of
attendance never arrived. The regular connector (`avora_biometric.py`) is
incremental and reads Smart Office's SQL Server on that one PC; this reads a
source you can reach from anywhere and sends exactly the days you ask for.

    # from an export (any machine with internet)
    python backfill.py --from 2026-10-01 --to 2026-10-31 --csv october.csv --dry-run
    python backfill.py --from 2026-10-01 --to 2026-10-31 --csv october.csv

    # straight off the device (any machine on the office LAN; pip install pyzk)
    python backfill.py --from 2026-10-01 --to 2026-10-31 --zk 192.168.1.201 --dry-run

ALWAYS --dry-run first. It reads the source, applies the range, and prints what
WOULD be sent - punch count, people, and a per-day tally - without touching
Avora. A device log that turns out to be empty, or in the wrong timezone, or
keyed on ids nobody set in Avora, shows up there instead of in your attendance.

Re-running is safe: the server upserts ONE work session per employee-day, so the
same punches produce the same result. But it IS the formal attendance record, so
keep the range to the month you mean - re-sending an older month would rewrite
days that payroll may already have been run against.

Config comes from the environment or `avora-config.json` next to this script,
the same file the regular connector uses: AVORA_API_URL and the HMAC secret
BIOMETRIC_WEBHOOK_SECRET, which must match the backend's value exactly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "avora-config.json"
DEFAULT_API_URL = "https://api.avora.optiminastic.com"
# The server's own cap is 5000 per request; stay well under it so a slow link
# does not time out mid-month with half a batch sent.
BATCH_SIZE = 500

# Export files name these columns a dozen different ways. Lowercased, stripped
# of spaces and underscores, these are the ones seen in the wild.
ID_COLUMNS = ("externalid", "userid", "employeecode", "empcode", "enrollid", "id")
TIME_COLUMNS = ("punchedat", "logdate", "punchtime", "datetime", "time", "attendancedate")


@dataclass(frozen=True)
class Punch:
    external_id: str
    punched_at: datetime


def _config(key: str, fallback: str = "") -> str:
    """Environment first, then the shared config file. Never printed."""
    value = os.environ.get(key)
    if value:
        return value.strip()
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            sys.exit(f"{CONFIG_FILE.name} is not valid JSON: {exc}")
        found = data.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip()
    return fallback


def _pick(header: list[str], candidates: tuple[str, ...]) -> str | None:
    normalised = {h.lower().replace(" ", "").replace("_", ""): h for h in header}
    for candidate in candidates:
        if candidate in normalised:
            return normalised[candidate]
    return None


def _parse_time(raw: str) -> datetime | None:
    """ISO first, then the formats Indian biometric middleware tends to emit."""
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def read_attlog(path: Path) -> list[Punch]:
    """A terminal's own USB export (`1_attlog.dat`): no header, tab separated,
    `userid <tab> YYYY-MM-DD HH:MM:SS <tab> status ...`.

    This is the only way off a push-style terminal whose server is down - it
    writes the file straight to a pen drive from the device menu.
    """
    punches, unparsed = [], 0
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            unparsed += 1
            continue
        external = parts[0].strip()
        # Tab-separated keeps the timestamp in one field; whitespace-split
        # breaks it into date and time, so stitch those back together.
        stamp = _parse_time(parts[1] if "\t" in line else " ".join(parts[1:3]))
        if not external or stamp is None:
            unparsed += 1
            continue
        punches.append(Punch(external_id=external, punched_at=stamp))
    if unparsed:
        print(f"note: skipped {unparsed} line(s) that were not a punch")
    return punches


def looks_like_attlog(path: Path) -> bool:
    """Headerless and tab separated, with a timestamp in the second field."""
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        first = handle.readline()
    if "," in first or "\t" not in first:
        return False
    parts = first.split("\t")
    return len(parts) >= 2 and _parse_time(parts[1]) is not None


def read_csv(path: Path) -> list[Punch]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            sys.exit(f"{path} has no header row.")
        id_col = _pick(list(reader.fieldnames), ID_COLUMNS)
        time_col = _pick(list(reader.fieldnames), TIME_COLUMNS)
        if id_col is None or time_col is None:
            sys.exit(
                f"Could not find the columns in {path}.\n"
                f"  found:    {', '.join(reader.fieldnames)}\n"
                f"  need one of {ID_COLUMNS} and one of {TIME_COLUMNS}\n"
                "Rename the two columns to external_id and punched_at and retry."
            )
        punches, unparsed = [], 0
        for row in reader:
            external = (row.get(id_col) or "").strip()
            stamp = _parse_time(row.get(time_col) or "")
            if not external or stamp is None:
                unparsed += 1
                continue
            punches.append(Punch(external_id=external, punched_at=stamp))
    if unparsed:
        print(f"note: skipped {unparsed} row(s) with a missing id or an unreadable time")
    return punches


def read_device(host: str, port: int, password: int) -> list[Punch]:
    try:
        from zk import ZK  # type: ignore[import-untyped]
    except ImportError:
        sys.exit("Reading the device needs pyzk - run: pip install pyzk")
    print(f"connecting to {host}:{port} ...")
    conn = ZK(host, port=port, password=password, timeout=15).connect()
    try:
        records = conn.get_attendance() or []
    finally:
        conn.disconnect()
    return [Punch(external_id=str(r.user_id), punched_at=r.timestamp) for r in records]


def in_range(punches: list[Punch], start: date, end: date) -> list[Punch]:
    """Inclusive, on the punch's own local date - these timestamps are office
    time as the device recorded them, which is what the server buckets by."""
    return sorted(
        (p for p in punches if start <= p.punched_at.date() <= end),
        key=lambda p: p.punched_at,
    )


def describe(punches: list[Punch]) -> None:
    if not punches:
        print("nothing in that range")
        return
    per_day = Counter(p.punched_at.date() for p in punches)
    print(
        f"\n{len(punches)} punches · {len({p.external_id for p in punches})} people · "
        f"{punches[0].punched_at} -> {punches[-1].punched_at}\n"
    )
    for day in sorted(per_day):
        print(f"  {day}  {per_day[day]:>4} punches")


def send(punches: list[Punch], api_url: str, secret: str) -> None:
    unmatched: set[str] = set()
    sessions = matched = 0
    for start in range(0, len(punches), BATCH_SIZE):
        chunk = punches[start : start + BATCH_SIZE]
        body = json.dumps(
            {
                "punches": [
                    {"external_id": p.external_id, "punched_at": p.punched_at.isoformat()}
                    for p in chunk
                ]
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - our own configured host
            f"{api_url.rstrip('/')}/api/v1/attendance/biometric",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Biometric-Signature": hmac.new(
                    secret.encode(), body, hashlib.sha256
                ).hexdigest(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            sys.exit(f"\npush failed at punch {start} ({exc.code}): {detail}")
        except urllib.error.URLError as exc:
            sys.exit(f"\ncould not reach Avora: {exc.reason}")
        matched += int(result.get("matched", 0))
        sessions += int(result.get("sessions_upserted", 0))
        unmatched.update(result.get("unmatched_external_ids") or [])
        print(
            f"  sent {start + len(chunk):>5}/{len(punches)}  "
            f"matched {result.get('matched')}  sessions {result.get('sessions_upserted')}"
        )
    print(
        f"\ndone · {matched} of {len(punches)} punches matched an employee · "
        f"{sessions} employee-days written"
    )
    if unmatched:
        print(
            "\nThese device ids match nobody in Avora, so their attendance was NOT "
            "recorded.\nSet each person's Biometric ID on their profile and re-run:\n  "
            + ", ".join(sorted(unmatched))
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, metavar="YYYY-MM-DD")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--csv", type=Path, metavar="FILE", help="an export: CSV, or the device's attlog.dat"
    )
    source.add_argument("--zk", metavar="HOST", help="device IP on the office LAN")
    parser.add_argument("--zk-port", type=int, default=4370)
    parser.add_argument("--zk-password", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", help="read and report, send nothing")
    args = parser.parse_args()

    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as exc:
        sys.exit(f"dates must be YYYY-MM-DD: {exc}")
    if end < start:
        sys.exit("--to is before --from")

    if args.csv:
        if not args.csv.exists():
            sys.exit(f"no such file: {args.csv}")
        raw = read_attlog(args.csv) if looks_like_attlog(args.csv) else read_csv(args.csv)
    else:
        raw = read_device(args.zk, args.zk_port, args.zk_password)
    print(f"read {len(raw)} punches from the source")
    punches = in_range(raw, start, end)
    describe(punches)
    if not punches:
        sys.exit(0)
    if args.dry_run:
        print("\n--dry-run: nothing sent. Drop the flag to push these to Avora.")
        return

    secret = _config("BIOMETRIC_WEBHOOK_SECRET")
    if not secret:
        sys.exit(
            "No BIOMETRIC_WEBHOOK_SECRET. Set it in the environment or in "
            f"{CONFIG_FILE.name}; it must match the backend's value exactly."
        )
    print(f"\npushing to {_config('AVORA_API_URL', DEFAULT_API_URL)} ...")
    send(punches, _config("AVORA_API_URL", DEFAULT_API_URL), secret)


if __name__ == "__main__":
    main()
