#!/usr/bin/env python3
"""Avora biometric connector — runs on the office PC, pushes device punches to Avora.

It reads attendance punches from your biometric setup and POSTs them, HMAC-signed,
to Avora's `/api/v1/attendance/biometric` endpoint. Avora turns them into the
formal attendance record and reconciles them against laptop activity.

Two read sources (set SOURCE):
  - "zk"  : pull straight from a ZKTeco/eSSL-family device over the LAN (needs the
            `pyzk` package). Most eSSL/ZKTeco/Matrix devices speak this protocol.
  - "csv" : read a CSV the vendor middleware exports (stdlib only — no extra deps).

Everything else is the Python standard library. Configure via environment
variables (see config.example.env) and run once from Task Scheduler/cron, or with
`--loop 300` to poll every 5 minutes.

    python connector.py            # one sync pass
    python connector.py --loop 300 # poll forever, every 300s
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

API_URL = os.environ.get("AVORA_API_URL", "http://localhost:8000").rstrip("/")
SECRET = os.environ.get("BIOMETRIC_WEBHOOK_SECRET", "")
SOURCE = os.environ.get("SOURCE", "csv").lower()
STATE_FILE = os.environ.get("STATE_FILE", "biometric-state.json")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))

# CSV source: a file with columns external_id, punched_at[, direction].
CSV_PATH = os.environ.get("CSV_PATH", "punches.csv")

# ZK source: device address + an optional CSV mapping device user-id -> external
# id (usually the work email), so unmatched ids don't pile up on the server.
ZK_HOST = os.environ.get("ZK_HOST", "")
ZK_PORT = int(os.environ.get("ZK_PORT", "4370"))
ZK_PASSWORD = int(os.environ.get("ZK_PASSWORD", "0"))
MAPPING_CSV = os.environ.get("MAPPING_CSV", "")  # columns: device_id, external_id


@dataclass
class Punch:
    external_id: str
    punched_at: datetime  # office-local; Avora interprets naive times in office tz


# --------------------------------------------------------------------------- #
# State (last punch timestamp we've already sent) — avoids re-pushing forever.
# Server ingest is idempotent anyway, so this is just an efficiency guard.
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def _load_mapping() -> dict[str, str]:
    if not MAPPING_CSV:
        return {}
    mapping: dict[str, str] = {}
    with open(MAPPING_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            device_id = (row.get("device_id") or "").strip()
            external = (row.get("external_id") or "").strip()
            if device_id and external:
                mapping[device_id] = external
    return mapping


def read_csv_punches() -> list[Punch]:
    punches: list[Punch] = []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            external = (row.get("external_id") or "").strip()
            stamp = (row.get("punched_at") or "").strip()
            if not external or not stamp:
                continue
            punches.append(Punch(external_id=external, punched_at=datetime.fromisoformat(stamp)))
    return punches


def read_zk_punches() -> list[Punch]:
    try:
        from zk import ZK  # type: ignore[import-untyped]  # pip install pyzk
    except ImportError:
        sys.exit("SOURCE=zk needs pyzk — run: pip install pyzk")

    mapping = _load_mapping()
    conn = ZK(ZK_HOST, port=ZK_PORT, password=ZK_PASSWORD, timeout=10).connect()
    try:
        records = conn.get_attendance() or []
    finally:
        conn.disconnect()

    punches: list[Punch] = []
    for rec in records:
        device_id = str(rec.user_id)
        punches.append(
            Punch(external_id=mapping.get(device_id, device_id), punched_at=rec.timestamp)
        )
    return punches


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #
def post_batch(punches: list[Punch]) -> dict[str, object]:
    body = json.dumps(
        {
            "punches": [
                {"external_id": p.external_id, "punched_at": p.punched_at.isoformat()}
                for p in punches
            ]
        }
    ).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(  # noqa: S310 (our own configured https URL)
        f"{API_URL}/api/v1/attendance/biometric",
        data=body,
        headers={"Content-Type": "application/json", "X-Biometric-Signature": signature},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read())  # type: ignore[no-any-return]


def sync_once() -> None:
    if not SECRET:
        sys.exit("Set BIOMETRIC_WEBHOOK_SECRET (must match the Avora backend).")
    punches = read_zk_punches() if SOURCE == "zk" else read_csv_punches()

    watermark = _load_watermark()
    if watermark is not None:
        punches = [p for p in punches if p.punched_at > watermark]
    if not punches:
        print("no new punches")
        return

    punches.sort(key=lambda p: p.punched_at)
    newest = punches[-1].punched_at
    for i in range(0, len(punches), BATCH_SIZE):
        chunk = punches[i : i + BATCH_SIZE]
        try:
            result = post_batch(chunk)
        except urllib.error.HTTPError as exc:
            sys.exit(f"push failed ({exc.code}): {exc.read().decode(errors='replace')}")
        except urllib.error.URLError as exc:
            sys.exit(f"could not reach Avora: {exc.reason}")
        print(
            f"pushed {result.get('received')} punches · "
            f"matched {result.get('matched')} · "
            f"sessions {result.get('sessions_upserted')} · "
            f"unmatched {result.get('unmatched_external_ids')}"
        )
    _save_watermark(newest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Push biometric punches to Avora.")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever every N seconds")
    args = parser.parse_args()
    if args.loop:
        print(f"Avora biometric connector polling every {args.loop}s (source={SOURCE})")
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
