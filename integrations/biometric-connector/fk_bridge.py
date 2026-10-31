#!/usr/bin/env python3
"""Stand in for the Smart Office PC: take the terminal's pushes into Avora.

The Biomax N-BM30W (EBKN/FK "FkWeb" family) does not listen for a reader - it
dials out to ServerIP:ServerPort and posts what it has. When that server is off,
punches simply queue on the device and attendance silently stops. This is that
server, and it forwards straight to Avora, so nothing on the office LAN has to
stay alive.

Two rules the protocol does not forgive:

  * acknowledge every push with an EMPTY body and a `response_code: OK` header.
    Any other reply and the device re-sends the same record forever.
  * never put this behind Caddy or Nginx. Both drop headers containing
    underscores, and `request_code` / `dev_id` are how requests identify
    themselves. Bind the port directly.

Self-healing: a realtime push that is lost - Avora down, this restarted
mid-request - is not gone, because the device keeps its own log. On startup and
then every few hours the bridge asks for that log (`GET_LOG_DATA`) and forwards
anything newer than the last punch it has seen. A punch has to be missed by both
paths to be lost.

    python3 fk_bridge.py --port 8090

Config: AVORA_API_URL, BIOMETRIC_WEBHOOK_SECRET, FK_ALLOWED_DEV_IDS (comma
separated) from the environment or avora-config.json beside this file.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "avora-config.json"
SPOOL = HERE / "fk-spool.jsonl"  # punches Avora has not accepted yet
STATE = HERE / "fk-bridge-state.json"  # newest punch forwarded, for catch-up
LOG = HERE / "fk-bridge.log"

DEFAULT_API = "https://api.avora.optiminastic.com"
RECORD = 32
CATCHUP_SECONDS = 6 * 60 * 60
MAX_BATCH = 500


def config(key: str, fallback: str = "") -> str:
    value = os.environ.get(key)
    if value:
        return value.strip()
    if CONFIG_FILE.exists():
        try:
            found = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get(key)
        except json.JSONDecodeError:
            found = None
        if isinstance(found, str) and found.strip():
            return found.strip()
    return fallback


def log(line: str) -> None:
    stamped = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}"
    print(stamped, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(stamped + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Avora
# --------------------------------------------------------------------------- #
def post_to_avora(punches: list[dict[str, str]]) -> bool:
    """True once Avora has the batch. Failure is never fatal: the caller spools."""
    secret = config("BIOMETRIC_WEBHOOK_SECRET")
    if not secret:
        log("no BIOMETRIC_WEBHOOK_SECRET set - spooling instead of sending")
        return False
    body = json.dumps({"punches": punches}).encode()
    request = urllib.request.Request(  # noqa: S310 - our own configured host
        f"{config('AVORA_API_URL', DEFAULT_API).rstrip('/')}/api/v1/attendance/biometric",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Biometric-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            result = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log(f"Avora rejected or unreachable ({exc}) - spooled")
        return False
    unmatched = result.get("unmatched_external_ids") or []
    log(
        f"-> Avora: {result.get('matched')}/{len(punches)} matched, "
        f"{result.get('sessions_upserted')} employee-days"
        + (f", UNMATCHED ids {','.join(unmatched)}" if unmatched else "")
    )
    return True


def deliver(punches: list[dict[str, str]]) -> None:
    """Send, and keep anything that did not land so a later run retries it."""
    if not punches:
        return
    pending = read_spool() + punches
    batch = pending[:MAX_BATCH]
    # One send per call, whatever the outcome. Anything past MAX_BATCH - and
    # everything, if the send failed - stays spooled for the next punch or
    # catch-up to carry. Avora's ingest is idempotent, so a retry is safe, but
    # posting the same batch twice in one call was still wasted work.
    if post_to_avora(batch):
        write_spool(pending[MAX_BATCH:])
        remember_newest(batch)
    else:
        write_spool(pending)


def read_spool() -> list[dict[str, str]]:
    if not SPOOL.exists():
        return []
    lines = SPOOL.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def write_spool(punches: list[dict[str, str]]) -> None:
    SPOOL.write_text("".join(json.dumps(p) + "\n" for p in punches), encoding="utf-8")


def remember_newest(punches: list[dict[str, str]]) -> None:
    newest = max((p["punched_at"] for p in punches), default="")
    if newest > config_state():
        STATE.write_text(json.dumps({"newest": newest}), encoding="utf-8")


def config_state() -> str:
    if not STATE.exists():
        return ""
    try:
        return str(json.loads(STATE.read_text(encoding="utf-8")).get("newest", ""))
    except json.JSONDecodeError:
        return ""


# --------------------------------------------------------------------------- #
# The device's stored log
# --------------------------------------------------------------------------- #
def decode_log(blob: bytes, count: int) -> list[dict[str, str]]:
    """Fixed 32-byte records. Layout worked out against the device and checked
    against punches it had separately sent as JSON - see decode notes in the
    repo. byte 4..19 user id, byte 23 seconds, byte 24..27 packs
    minute/hour/day/month and a 1900-based year."""
    punches: list[dict[str, str]] = []
    for index in range(count):
        record = blob[index * RECORD : index * RECORD + RECORD]
        if len(record) < RECORD:
            break
        user = record[4:20].split(b"\x00")[0].decode("ascii", errors="replace").strip()
        if not user:
            continue
        word = struct.unpack("<I", record[24:28])[0]
        try:
            when = datetime(
                1900 + ((word >> 2) & 0x3FF),
                (word >> 12) & 0xF,
                (word >> 16) & 0x1F,
                (word >> 21) & 0x1F,
                (word >> 26) & 0x3F,
                record[23],
            )
        except ValueError:
            continue
        punches.append({"external_id": user, "punched_at": when.isoformat()})
    return punches


class Bridge:
    """Server state: what to ask the device next, and what it has sent."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.lock = threading.Lock()
        self.commands: list[str] = ["GET_LOG_DATA"]  # catch up on first poll
        self.blocks: list[bytes] = []
        self.expect_log = True
        self.trans = 1000
        self.last_catchup = time.time()

    def due_for_catchup(self) -> bool:
        return time.time() - self.last_catchup > CATCHUP_SECONDS

    def queue_catchup(self) -> None:
        with self.lock:
            if "GET_LOG_DATA" not in self.commands:
                self.commands.append("GET_LOG_DATA")
                self.expect_log = True
                self.blocks = []
            self.last_catchup = time.time()

    def finish_log(self) -> None:
        """Assemble the blocks, forward only what is newer than we have."""
        with self.lock:
            blocks, self.blocks, self.expect_log = self.blocks, [], False
        if not blocks:
            return
        first = blocks[0]
        header_len = struct.unpack("<I", first[:4])[0]
        try:
            header = json.loads(first[4 : 4 + header_len].split(b"\x00")[0].strip())
        except (json.JSONDecodeError, struct.error):
            log("log dump header unreadable - ignoring")
            return
        blob = first[4 + header_len :] + b"".join(blocks[1:])
        punches = decode_log(blob, int(header.get("log_count", 0)))
        since = config_state()
        if not since:
            # First run. The device remembers YEARS - this one held back to 2022
            # - and replaying all of it would rewrite attendance for months that
            # are long closed, payroll included. So the first dump only
            # establishes where we are; history is a deliberate act, via
            # backfill.py with an explicit date range.
            remember_newest(punches)
            log(
                f"first run: learned the high-water mark from {len(punches)} stored records "
                f"({config_state()}). Nothing sent - use backfill.py for history on purpose."
            )
            return
        fresh = [p for p in punches if p["punched_at"] > since]
        log(f"stored log: {len(punches)} records, {len(fresh)} newer than {since}")
        for start in range(0, len(fresh), MAX_BATCH):
            deliver(fresh[start : start + MAX_BATCH])


def punch_from_push(body: bytes) -> dict[str, str] | None:
    match = re.search(rb"\{[^\x00]*?\}", body, re.S)
    if not match:
        return None
    try:
        record = json.loads(match.group(0))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    raw = str(record.get("io_time", ""))
    if len(raw) != 14 or not raw.isdigit() or "user_id" not in record:
        return None
    when = datetime.strptime(raw, "%Y%m%d%H%M%S")
    return {"external_id": str(record["user_id"]), "punched_at": when.isoformat()}


def make_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a: object) -> None:
            """Quiet; this keeps its own log."""

        def _ok(self, extra: dict[str, str] | None = None) -> None:
            self.send_response(200)
            self.send_header("response_code", "OK")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_GET(self) -> None:
            self._ok()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            code = self.headers.get("request_code", "")
            device = self.headers.get("dev_id", "")

            # Nothing here is authenticated by the protocol, so the device id is
            # the only identity on offer. Treat anything else as noise.
            if bridge.allowed and device not in bridge.allowed:
                log(f"ignored a push from unknown dev_id {device!r} ({self.client_address[0]})")
                self._ok()
                return

            if code == "receive_cmd":
                if bridge.due_for_catchup():
                    bridge.queue_catchup()
                with bridge.lock:
                    pending = bridge.commands.pop(0) if bridge.commands else None
                    bridge.trans += 1
                    trans = str(bridge.trans)
                if pending:
                    log(f"asking the device for {pending}")
                    self._ok({"cmd_code": pending, "trans_id": trans})
                else:
                    self._ok()
                return

            if code == "send_cmd_result":
                with bridge.lock:
                    bridge.blocks.append(body)
                self._ok()
                return

            if code == "realtime_glog":
                punch = punch_from_push(body)
                if punch:
                    log(f"punch: user {punch['external_id']} at {punch['punched_at']}")
                    deliver([punch])
                self._ok()
                return

            self._ok()

        def finish(self) -> None:
            super().finish()
            # A log dump arrives as a run of blocks and ends in silence, so the
            # end of one is only visible once the pushes go quiet again.
            if (
                bridge.expect_log
                and bridge.blocks
                and self.headers.get("request_code") != "send_cmd_result"
            ):
                bridge.finish_log()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge an FK/EBKN terminal into Avora.")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--dev-id", action="append", default=[], help="allowed device id (repeatable)"
    )
    parser.add_argument(
        "--flush",
        action="store_true",
        help="send anything already spooled, then exit (does not start the server)",
    )
    args = parser.parse_args()

    if args.flush:
        # Spooled punches normally go out on the back of the next one to arrive.
        # That is fine for a terminal that punches all day, but it means a
        # backlog can sit there overnight, and it gives no way to confirm the
        # push path works without waiting for a human to touch the sensor.
        waiting = read_spool()
        if not waiting:
            log("nothing spooled")
            return
        write_spool([])
        deliver(waiting)
        remaining = len(read_spool())
        log(f"flush done: {len(waiting) - remaining} delivered, {remaining} still spooled")
        return

    allowed = set(args.dev_id) | {
        d.strip() for d in config("FK_ALLOWED_DEV_IDS").split(",") if d.strip()
    }
    bridge = Bridge(allowed)
    log(f"bridge up on 0.0.0.0:{args.port} -> {config('AVORA_API_URL', DEFAULT_API)}")
    log(f"accepting device ids: {', '.join(sorted(allowed)) or 'ANY (set FK_ALLOWED_DEV_IDS)'}")
    if read_spool():
        log(f"{len(read_spool())} spooled punches to retry")
    # The terminal connects from the office; this has to answer on the wire it
    # arrives on, so binding every interface is the point, not an oversight.
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(bridge)).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
