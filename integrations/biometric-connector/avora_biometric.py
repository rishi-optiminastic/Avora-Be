#!/usr/bin/env python3
"""Avora biometric connector — SINGLE-FILE edition (Smart Office / SQL Server).

Reads punches straight from Smart Office's local SQL Server database
(SmartOfficedb) and pushes them, HMAC-signed, to Avora. No device, no network to
the terminal — runs right on the Smart Office PC. Just edit the two CONFIG lines
below and run:

    python avora_biometric.py            # one sync pass (test)
    python avora_biometric.py --loop 300 # poll forever, every 5 minutes

On first run it auto-installs pyodbc.

How matching works: each punch's UserId is sent as `external_id`; Avora maps it to
the employee whose Biometric ID equals that number. Set Biometric ID per person in
Avora (Admin -> profile). New joiners just need their Biometric ID set — no edits
here.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# ======================== EDIT THESE TWO LINES ============================== #
AVORA_API_URL = "https://avora-be.onrender.com"   # your deployed backend URL
BIOMETRIC_WEBHOOK_SECRET = "PASTE-THE-SAME-SECRET-YOU-SET-ON-RENDER"
# =========================================================================== #

# Smart Office SQL Server (found via discovery — Windows auth, no password).
SQL_SERVER = "localhost"
SQL_DATABASE = "SmartOfficedb"

# Punches live in monthly tables DeviceLogs_<month>_<year> (e.g. DeviceLogs_6_2026).
TABLE_PREFIX = "DeviceLogs"

BATCH_SIZE = 500
STATE_FILE = "biometric-state.json"   # remembers the last punch already sent

# Only send punches on/after this date. "" = from the 1st of the CURRENT month
# (skips the historical backlog). Override like "2026-06-01" if you want.
SEND_SINCE = ""
# --------------------------------------------------------------------------- #


def _ensure(pkg: str) -> None:
    try:
        importlib.import_module(pkg)
    except ImportError:
        print(f"Installing {pkg} (one-time)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])


def _cutoff() -> datetime:
    if SEND_SINCE:
        return datetime.fromisoformat(SEND_SINCE)
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _load_watermark() -> datetime | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh).get("last_punch")
            return datetime.fromisoformat(raw) if raw else None
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def _save_watermark(when: datetime) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"last_punch": when.isoformat()}, fh)


def read_punches() -> list[tuple[str, datetime]]:
    """Read (UserId, LogDate) from the current + previous month DeviceLogs tables."""
    import pyodbc

    driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
    conn = pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};Trusted_Connection=yes;",
        timeout=10,
    )
    cur = conn.cursor()

    now = datetime.now()
    prev = now.replace(day=1) - timedelta(days=1)
    months = [(now.year, now.month), (prev.year, prev.month)]

    punches: list[tuple[str, datetime]] = []
    for year, month in months:
        table = f"{TABLE_PREFIX}_{month}_{year}"
        try:
            cur.execute(f"SELECT UserId, LogDate FROM [{table}]")  # noqa: S608 (fixed table name)
        except pyodbc.Error:
            continue  # table for that month doesn't exist yet — fine
        for user_id, log_date in cur.fetchall():
            if user_id is None or log_date is None:
                continue
            punches.append((str(user_id).strip(), log_date))
    conn.close()
    return punches


def post_batch(punches: list[tuple[str, datetime]]) -> dict[str, object]:
    body = json.dumps(
        {"punches": [{"external_id": eid, "punched_at": ts.isoformat()} for eid, ts in punches]}
    ).encode()
    signature = hmac.new(BIOMETRIC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{AVORA_API_URL.rstrip('/')}/api/v1/attendance/biometric",
        data=body,
        headers={"Content-Type": "application/json", "X-Biometric-Signature": signature},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read())  # type: ignore[no-any-return]


def sync_once() -> None:
    if "PASTE" in BIOMETRIC_WEBHOOK_SECRET or not BIOMETRIC_WEBHOOK_SECRET:
        sys.exit("Edit BIOMETRIC_WEBHOOK_SECRET at the top (must match Render).")

    punches = read_punches()

    # Floor at the cutoff (this month) AND the watermark (last sent), whichever is later.
    floor = _cutoff()
    watermark = _load_watermark()
    if watermark is not None and watermark > floor:
        floor = watermark
    punches = [p for p in punches if p[1] > floor]
    if not punches:
        print("no new punches")
        return

    punches.sort(key=lambda p: p[1])
    newest = punches[-1][1]
    print(f"sending {len(punches)} punch(es)...")
    for i in range(0, len(punches), BATCH_SIZE):
        chunk = punches[i : i + BATCH_SIZE]
        try:
            result = post_batch(chunk)
        except urllib.error.HTTPError as exc:
            sys.exit(f"push failed ({exc.code}): {exc.read().decode(errors='replace')}")
        except urllib.error.URLError as exc:
            sys.exit(f"could not reach Avora: {exc.reason}")
        print(
            f"  received {result.get('received')} · matched {result.get('matched')} · "
            f"sessions {result.get('sessions_upserted')} · "
            f"unmatched {result.get('unmatched_external_ids')}"
        )
    _save_watermark(newest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Push Smart Office punches to Avora.")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever every N seconds")
    args = parser.parse_args()

    _ensure("pyodbc")
    if args.loop:
        print(f"Avora biometric connector — SmartOfficedb, every {args.loop}s. Ctrl+C to stop.")
        while True:
            try:
                sync_once()
            except Exception as exc:  # keep the loop alive across transient errors
                print(f"sync error: {exc}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        sync_once()


if __name__ == "__main__":
    main()
