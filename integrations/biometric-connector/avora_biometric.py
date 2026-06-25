#!/usr/bin/env python3
"""Avora biometric connector — SINGLE-FILE edition (Smart Office / SQL Server).

Reads punches straight from Smart Office's local SQL Server database
(SmartOfficedb) and pushes them, HMAC-signed, to Avora. No device, no network to
the terminal — runs right on the Smart Office PC. Edit the two CONFIG lines below
and run:

    python avora_biometric.py            # one sync pass (test)
    python avora_biometric.py --loop 300 # poll forever, every 5 minutes

On first run it auto-installs pyodbc.

How matching works: each punch's UserId is sent as `external_id`; Avora maps it to
the employee whose Biometric ID equals that number. Set Biometric ID per person in
Avora (Admin -> profile). New joiners just need their Biometric ID set — no edits
here.

Incremental sync: progress is tracked by the row's DownloadDate (when Smart Office
wrote it), NOT the punch time — because the device often uploads punches hours
late. A small overlap is re-sent each run to defeat ties; the Avora server is
idempotent (one session per employee-day) so re-sending is always safe.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# ======================== EDIT THESE TWO LINES ============================== #
AVORA_API_URL = "https://avora-be.onrender.com"   # your deployed backend URL
BIOMETRIC_WEBHOOK_SECRET = "PASTE-THE-SAME-SECRET-YOU-SET-ON-RENDER"  # noqa: S105 (placeholder)
# =========================================================================== #

# Smart Office SQL Server (found via discovery — Windows auth, no password).
SQL_SERVER = "localhost"
SQL_DATABASE = "SmartOfficedb"

# Punches live in monthly tables DeviceLogs_<month>_<year> (e.g. DeviceLogs_6_2026).
TABLE_PREFIX = "DeviceLogs"

BATCH_SIZE = 500
HTTP_TIMEOUT = 60                      # seconds; generous for Render cold starts
OVERLAP = timedelta(minutes=5)        # re-send recently-downloaded rows (beats ties/lag)
STATE_FILE = "biometric-state.json"   # remembers the last DownloadDate synced

# Only send punches whose punch time (LogDate) is on/after this. "" = from the 1st
# of the CURRENT month (skips the historical backlog). Override like "2026-06-01".
SEND_SINCE = ""
# --------------------------------------------------------------------------- #

# (external_id, punched_at, direction, download_at)
Punch = tuple[str, datetime, str, datetime]


def _ensure(pkg: str) -> None:
    try:
        importlib.import_module(pkg)
    except ImportError:
        import subprocess

        print(f"Installing {pkg} (one-time)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])  # noqa: S603


def _cutoff() -> datetime:
    """Earliest punch time (LogDate) to consider — skips the historical backlog."""
    if SEND_SINCE:
        return datetime.fromisoformat(SEND_SINCE)
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _load_watermark() -> datetime | None:
    """Last DownloadDate we've already synced (None on first run / fresh state)."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh).get("last_download")
            return datetime.fromisoformat(raw) if raw else None
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def _save_watermark(when: datetime) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"last_download": when.isoformat()}, fh)


def read_punches() -> list[Punch]:
    """Read every punch from the current + previous month DeviceLogs tables as
    (UserId, LogDate, direction, DownloadDate). Direction is 'in' / 'out' / 'auto'."""
    import pyodbc

    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if not drivers:
        raise RuntimeError("No 'SQL Server' ODBC driver found on this PC.")
    conn = pyodbc.connect(
        f"DRIVER={{{drivers[-1]}}};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};Trusted_Connection=yes;",
        timeout=10,
    )
    try:
        cur = conn.cursor()
        now = datetime.now()
        prev = now.replace(day=1) - timedelta(days=1)
        months = [(now.year, now.month), (prev.year, prev.month)]

        rows: list[Punch] = []
        for year, month in months:
            table = f"{TABLE_PREFIX}_{month}_{year}"
            try:
                cur.execute(
                    f"SELECT UserId, LogDate, Direction, DownloadDate FROM [{table}]"  # noqa: S608
                )
            except pyodbc.Error:
                continue  # table for that month doesn't exist yet — fine
            for user_id, log_date, direction, dl_date in cur.fetchall():
                if user_id is None or log_date is None:
                    continue
                d = (direction or "").strip().lower()
                d = d if d in ("in", "out") else "auto"
                rows.append((str(user_id).strip(), log_date, d, dl_date or log_date))
        return rows
    finally:
        conn.close()


def post_batch(punches: list[Punch]) -> dict[str, object]:
    body = json.dumps(
        {
            "punches": [
                {"external_id": eid, "punched_at": log.isoformat(), "direction": d}
                for eid, log, d, _dl in punches
            ]
        }
    ).encode()
    signature = hmac.new(BIOMETRIC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(  # noqa: S310 (our own configured https URL)
        f"{AVORA_API_URL.rstrip('/')}/api/v1/attendance/biometric",
        data=body,
        headers={"Content-Type": "application/json", "X-Biometric-Signature": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"push failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Avora: {exc.reason}") from exc


def sync_once() -> None:
    if "PASTE" in BIOMETRIC_WEBHOOK_SECRET or not BIOMETRIC_WEBHOOK_SECRET:
        raise SystemExit("Edit BIOMETRIC_WEBHOOK_SECRET at the top (must match Render).")

    rows = read_punches()
    cutoff = _cutoff()                         # skip punches older than this (by punch time)
    watermark = _load_watermark()              # last DownloadDate already synced
    dl_floor = (watermark - OVERLAP) if watermark else None

    selected = [
        p
        for p in rows
        if p[1] >= cutoff and (dl_floor is None or p[3] > dl_floor)
    ]
    if not selected:
        print("no new punches")
        return

    newest_dl = max(p[3] for p in selected)
    selected.sort(key=lambda p: p[1])
    print(f"sending {len(selected)} punch(es)...")
    for i in range(0, len(selected), BATCH_SIZE):
        chunk = selected[i : i + BATCH_SIZE]
        result = post_batch(chunk)  # raises on failure → caller decides whether to retry
        print(
            f"  received {result.get('received')} · matched {result.get('matched')} · "
            f"sessions {result.get('sessions_upserted')} · "
            f"unmatched {result.get('unmatched_external_ids')}"
        )
    _save_watermark(newest_dl)  # only after every batch landed


def main() -> None:
    parser = argparse.ArgumentParser(description="Push Smart Office punches to Avora.")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever every N seconds")
    args = parser.parse_args()

    _ensure("pyodbc")
    if not args.loop:
        try:
            sync_once()
        except KeyboardInterrupt:
            print("\nstopped.")
        return

    print(f"Avora biometric connector — SmartOfficedb, every {args.loop}s. Ctrl+C to stop.")
    try:
        while True:
            try:
                sync_once()
            except SystemExit:
                raise  # fatal config (bad secret) — stop the loop
            except Exception as exc:  # transient (DB/network): log & keep going
                print(f"sync error: {exc}", file=sys.stderr)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
