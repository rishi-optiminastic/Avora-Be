#!/usr/bin/env python3
"""Avora biometric connector - SINGLE-FILE edition (Smart Office / SQL Server).

Reads punches straight from Smart Office's local SQL Server database
(SmartOfficedb) and pushes them, HMAC-signed, to Avora. No device, no network to
the terminal - runs right on the Smart Office PC.

    python avora_biometric.py                 # one sync pass, then exit
    python avora_biometric.py --selftest      # check every prerequisite, change nothing
    python avora_biometric.py --status        # when did it last succeed?
    python avora_biometric.py --loop 300      # poll forever (manual/debug only)

HOW IT IS MEANT TO RUN
----------------------
As a SHORT ONE-SHOT, every few minutes, from a Windows Scheduled Task - not as a
long-lived `--loop` daemon. Run `install-windows.ps1` and it is set up correctly.

A daemon on Windows is fragile in ways that are invisible until attendance is
simply missing: Task Scheduler's default `ExecutionTimeLimit` is 3 days, so it
kills a `--loop` process and, with an "At startup" trigger, never starts it again
until the next reboot. A crash does the same. A one-shot that exits in seconds
cannot be killed for running too long, cannot leak, and if any single run fails
the next tick retries it five minutes later with no one watching. The watermark
makes repeats cheap and the server is idempotent, so re-running is always safe.

Matching: each punch's UserId is sent as `external_id`; Avora maps it to the
employee whose Biometric ID equals that number. Set Biometric ID per person in
Avora (Admin -> profile). New joiners just need their Biometric ID set.

Incremental sync: progress is tracked by the row's DownloadDate (when Smart
Office wrote it), NOT the punch time - the device often uploads punches hours
late. A small overlap is re-sent each run to defeat ties.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ============================= CONFIGURATION ================================ #
# Read from the environment or `avora-config.json` next to this script, so the
# HMAC secret is not carried in source. The fallbacks keep an existing install
# working untouched, but a committed secret is a leaked secret: set it properly
# and rotate (see README, "Rotating the webhook secret").
DEFAULT_API_URL = "https://api.avora.optiminastic.com"
CONFIG_FILE = HERE / "avora-config.json"


# Set when the config file exists but could not be parsed. It is reported from
# `main()` rather than raised here: this runs at import, BEFORE logging is set
# up, so raising would kill the process with nothing written to the log and no
# status recorded - under the scheduled task (--quiet, no console) it would die
# every 5 minutes leaving no trace at all, which is the exact silent failure
# this rewrite exists to remove.
CONFIG_ERROR: str | None = None

_ENV_KEYS = (
    "AVORA_API_URL",
    "BIOMETRIC_WEBHOOK_SECRET",
    "SQL_SERVER",
    "SQL_DATABASE",
    "SEND_SINCE",
)


def _config() -> dict[str, str]:
    """Settings, most trusted source first: environment, then config file."""
    global CONFIG_ERROR
    data: dict[str, str] = {}
    if CONFIG_FILE.is_file():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            data = {str(k): str(v) for k, v in loaded.items() if v is not None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            CONFIG_ERROR = f"{CONFIG_FILE.name} is not readable JSON: {exc}"
    for key in _ENV_KEYS:
        if os.environ.get(key):
            data[key] = os.environ[key]
    return data


CFG = _config()
AVORA_API_URL = CFG.get("AVORA_API_URL", DEFAULT_API_URL)
BIOMETRIC_WEBHOOK_SECRET = CFG.get("BIOMETRIC_WEBHOOK_SECRET", "")

# Smart Office SQL Server (Windows auth, no password).
SQL_SERVER = CFG.get("SQL_SERVER", "localhost")
SQL_DATABASE = CFG.get("SQL_DATABASE", "SmartOfficedb")

# Punches live in monthly tables DeviceLogs_<month>_<year> (e.g. DeviceLogs_6_2026).
TABLE_PREFIX = "DeviceLogs"

BATCH_SIZE = 500
HTTP_TIMEOUT = 60  # seconds
HTTP_ATTEMPTS = 3  # retries inside one run, so a blip does not wait for the next tick
OVERLAP = timedelta(minutes=5)  # re-send recently-downloaded rows (beats ties/lag)

STATE_FILE = HERE / "biometric-state.json"  # watermark: last DownloadDate synced
STATUS_FILE = HERE / "biometric-status.json"  # last outcome, for --status and monitoring
LOCK_FILE = HERE / "biometric.lock"
LOG_FILE = HERE / "avora_biometric.log"
LOG_MAX_BYTES = 2_000_000  # the log is rotated, so it can never fill the disk
LOG_BACKUPS = 3

# On a FIRST run only, ignore punches older than the 1st of the current month so
# the historical backlog is not replayed. Once a watermark exists it bounds the
# sync instead - see `_select`.
FIRST_RUN_SKIP_BEFORE = CFG.get("SEND_SINCE", "")

# (external_id, punched_at, direction, download_at)
Punch = tuple[str, datetime, str, datetime]

log = logging.getLogger("avora.biometric")


def _setup_logging(verbose: bool = True) -> None:
    """Log to a size-capped rotating file and to stdout.

    The file is rotated because this runs unattended for years; an append-forever
    log eventually fills the disk of the one PC attendance depends on.
    """
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        rotating = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        log.addHandler(rotating)
    except OSError:
        pass  # a read-only folder must not stop the sync itself
    if verbose:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        log.addHandler(stream)


class SingleInstance:
    """Refuse to run while another copy is mid-sync.

    Task Scheduler's IgnoreNew already stops the task overlapping itself, but a
    hand-run `python avora_biometric.py` during a scheduled tick would otherwise
    race it and both would advance the watermark.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: object | None = None

    def __enter__(self) -> bool:
        try:
            fh = open(self._path, "w", encoding="utf-8")
        except OSError:
            return True  # cannot lock -> do not block the sync
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def __exit__(self, *_exc: object) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        try:
            fh.close()  # type: ignore[attr-defined]
        except OSError:
            pass


def _ensure(pkg: str) -> None:
    try:
        importlib.import_module(pkg)
    except ImportError:
        import subprocess

        log.info("installing %s (one-time)", pkg)
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])  # noqa: S603


def _write_status(ok: bool, detail: str, sent: int = 0, matched: int = 0) -> None:
    """Record the outcome of this run so health can be read without the log.

    Silent failure is the real enemy here: nobody notices a dead connector until
    a month of attendance is missing. `--status` reads this file.
    """
    previous: dict[str, object] = {}
    try:
        previous = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    now = datetime.now().isoformat(timespec="seconds")
    status = {
        "last_run_at": now,
        "last_run_ok": ok,
        "last_detail": detail,
        "last_success_at": now if ok else previous.get("last_success_at"),
        "punches_sent": sent,
        "punches_matched": matched,
        "host": socket.gethostname(),
    }
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_watermark() -> datetime | None:
    """Last DownloadDate already synced (None on first run / fresh state)."""
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_download")
        return datetime.fromisoformat(raw) if raw else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_watermark(when: datetime) -> None:
    STATE_FILE.write_text(json.dumps({"last_download": when.isoformat()}), encoding="utf-8")


def _first_run_cutoff() -> datetime:
    """Earliest punch time to consider on a FIRST run - skips the old backlog."""
    if FIRST_RUN_SKIP_BEFORE:
        return datetime.fromisoformat(FIRST_RUN_SKIP_BEFORE)
    return datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _select(rows: list[Punch], watermark: datetime | None) -> list[Punch]:
    """The punches this run should send.

    Once a watermark exists it is the ONLY bound: everything downloaded since it
    goes, whatever month the punch falls in. The month cutoff used to apply on
    every run, which quietly dropped real attendance - if the connector was down
    from 28 Sep to 3 Oct, the cutoff moved to 1 Oct and the 29th and 30th were
    filtered out even though they had never been sent, and the watermark then
    advanced past them for good.
    """
    if watermark is not None:
        floor = watermark - OVERLAP
        return [p for p in rows if p[3] > floor]
    cutoff = _first_run_cutoff()
    return [p for p in rows if p[1] >= cutoff]


def read_punches() -> list[Punch]:
    """Every punch in the current + previous month DeviceLogs tables as
    (UserId, LogDate, direction, DownloadDate). Direction is 'in'/'out'/'auto'."""
    import pyodbc

    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if not drivers:
        raise RuntimeError("No 'SQL Server' ODBC driver found on this PC.")
    conn = pyodbc.connect(
        f"DRIVER={{{drivers[-1]}}};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};"
        "Trusted_Connection=yes;",
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
                continue  # table for that month does not exist yet - fine
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
    """POST one signed batch, retrying a transient failure a few times.

    A 4xx is the server rejecting what we sent (bad signature, bad shape): retrying
    cannot help, so it fails fast and loudly instead of burning the run.
    """
    body = json.dumps(
        {
            "punches": [
                {"external_id": eid, "punched_at": log_at.isoformat(), "direction": d}
                for eid, log_at, d, _dl in punches
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
    last: Exception | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            if 400 <= exc.code < 500:
                raise RuntimeError(f"push rejected ({exc.code}): {detail}") from exc
            last = RuntimeError(f"push failed ({exc.code}): {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = RuntimeError(f"could not reach Avora: {exc}")
        if attempt < HTTP_ATTEMPTS:
            wait = 2**attempt
            log.warning("%s - retrying in %ss (%d/%d)", last, wait, attempt, HTTP_ATTEMPTS)
            time.sleep(wait)
    raise last or RuntimeError("push failed")


def _as_int(value: object) -> int:
    """Coerce one field of the server's JSON reply. The reply is outside data,
    so its shape is a claim: a surprising type must not crash the sync."""
    return value if isinstance(value, int) else 0


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def sync_once() -> None:
    if not BIOMETRIC_WEBHOOK_SECRET or "PASTE" in BIOMETRIC_WEBHOOK_SECRET:
        raise SystemExit(
            "BIOMETRIC_WEBHOOK_SECRET is not set. Put it in avora-config.json next to "
            "this script (or in the environment); it must match the backend's."
        )

    rows = read_punches()
    watermark = _load_watermark()
    selected = _select(rows, watermark)
    if not selected:
        log.info("no new punches (%d row(s) scanned)", len(rows))
        _write_status(True, "no new punches")
        return

    newest_dl = max(p[3] for p in selected)
    selected.sort(key=lambda p: p[1])
    log.info("sending %d punch(es)", len(selected))

    matched = 0
    for i in range(0, len(selected), BATCH_SIZE):
        chunk = selected[i : i + BATCH_SIZE]
        result = post_batch(chunk)  # raises on failure, before the watermark moves
        matched += _as_int(result.get("matched"))
        unmatched = _as_list(result.get("unmatched_external_ids"))
        log.info(
            "received %s - matched %s - sessions %s - unmatched %s",
            result.get("received"),
            result.get("matched"),
            result.get("sessions_upserted"),
            unmatched,
        )
        if unmatched:
            log.warning(
                "%d id(s) have no employee in Avora: %s - set their Biometric ID",
                len(unmatched),
                unmatched,
            )
    _save_watermark(newest_dl)  # only after every batch landed
    _write_status(True, f"sent {len(selected)}", sent=len(selected), matched=matched)


def selftest() -> int:
    """Check every prerequisite and report. Run this first when it breaks.

    Read-only apart from installing pyodbc if it is missing, which is a
    prerequisite rather than a change to how the connector behaves.
    """
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        log.info("[%s] %s%s", "PASS" if ok else "FAIL", label, f" - {detail}" if detail else "")

    log.info("python %s", sys.version.split()[0])
    log.info("running as %s on %s", os.environ.get("USERNAME", "?"), socket.gethostname())
    check("webhook secret configured", bool(BIOMETRIC_WEBHOOK_SECRET))

    try:
        _ensure("pyodbc")
        import pyodbc

        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        check("ODBC driver present", bool(drivers), ", ".join(drivers) or "none found")
    except Exception as exc:
        check("pyodbc importable", False, str(exc))
        drivers = []

    if drivers:
        try:
            rows = read_punches()
            check(f"{SQL_DATABASE} readable", True, f"{len(rows)} punch row(s) this/last month")
        except Exception as exc:
            check(
                f"{SQL_DATABASE} readable",
                False,
                f"{exc} - the scheduled task must run as an account with rights on this database",
            )

    url = f"{AVORA_API_URL.rstrip('/')}/api/v1/healthz"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            check("Avora reachable", resp.status == 200, f"{url} -> {resp.status}")
    except Exception as exc:
        check("Avora reachable", False, f"{url} - {exc}")

    watermark = _load_watermark()
    log.info("watermark: %s", watermark.isoformat() if watermark else "none (first run)")
    log.info("%s", "ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 0 if failures == 0 else 1


def show_status() -> int:
    """Print the last outcome, and fail loudly if it is stale."""
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        log.error("no status yet - the connector has never completed a run on this PC")
        return 1
    for key in ("last_run_at", "last_run_ok", "last_success_at", "last_detail", "host"):
        log.info("%-16s %s", key, status.get(key))

    raw = status.get("last_success_at")
    if not raw:
        log.error("STALE: no successful sync has ever been recorded")
        return 1
    age = datetime.now() - datetime.fromisoformat(str(raw))
    if age > timedelta(minutes=30):
        log.error("STALE: last success was %s ago", str(age).split(".")[0])
        return 1
    log.info("healthy - last success %s ago", str(age).split(".")[0])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Smart Office punches to Avora.")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever (debug only)")
    parser.add_argument("--selftest", action="store_true", help="check every prerequisite")
    parser.add_argument("--status", action="store_true", help="report the last run's outcome")
    parser.add_argument("--quiet", action="store_true", help="log to file only")
    args = parser.parse_args()

    _setup_logging(verbose=not args.quiet)

    if CONFIG_ERROR and not args.status:
        log.error("%s", CONFIG_ERROR)
        _write_status(False, CONFIG_ERROR)
        return 2

    if args.status:
        return show_status()
    if args.selftest:
        return selftest()

    _ensure("pyodbc")

    if not args.loop:
        with SingleInstance(LOCK_FILE) as acquired:
            if not acquired:
                log.info("another sync is already running - skipping this tick")
                return 0
            try:
                sync_once()
                return 0
            except SystemExit as exc:  # fatal configuration
                log.error("%s", exc)
                _write_status(False, str(exc))
                return 2
            except Exception as exc:
                log.error("sync failed: %s", exc)
                _write_status(False, str(exc))
                return 1

    log.info("loop mode, every %ss - Ctrl+C to stop (prefer the scheduled task)", args.loop)
    try:
        while True:
            with SingleInstance(LOCK_FILE) as acquired:
                if acquired:
                    try:
                        sync_once()
                    except SystemExit as exc:
                        log.error("%s", exc)
                        _write_status(False, str(exc))
                        return 2
                    except Exception as exc:
                        log.error("sync error: %s", exc)
                        _write_status(False, str(exc))
            time.sleep(args.loop)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
