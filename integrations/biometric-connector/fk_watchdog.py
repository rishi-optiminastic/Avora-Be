#!/usr/bin/env python3
"""Email someone when the Biomax terminal stops talking to the bridge.

The bridge cannot lose a punch - the terminal retries until it is acknowledged,
failures spool, and a six-hourly catch-up re-reads the device's stored log. What
none of that survives is *silence*. When the terminal lost the bridge on 30
October 2026 the attendance board quietly fell back to laptop-agent data and it
was a day before anyone noticed; the same silence is why Zoho has a permanent
26-29 October gap.

Punch timestamps cannot tell you, either: a quiet afternoon looks exactly like a
severed link. So this watches contact, not punches. The terminal polls every few
seconds, so `fk-bridge-seen` going stale for minutes is unambiguous.

It mails through SMTP directly rather than asking Avora to send it, because the
backend is one of the things that may be down. Run it from a systemd timer; see
README-fk-bridge.md.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SEEN = HERE / "fk-bridge-seen"
STATE = HERE / "fk-watchdog-state.json"

TZ = ZoneInfo("Asia/Kolkata")
STALE_SECONDS = 20 * 60  # the device polls every few seconds
REMINDER_HOURS = 12  # re-nag while it is still down
# Only complain when the office is open. The terminal may legitimately be off
# overnight, and Avora's policy is a six-day week, so Sunday is not a fault.
WINDOW = (8, 20)
WORKING_DAYS = {0, 1, 2, 3, 4, 5}  # Mon-Sat


def read_state() -> dict[str, str]:
    try:
        loaded = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(state: dict[str, str]) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")


def last_contact() -> datetime | None:
    """When the terminal last reached the bridge, or None if it never has."""
    try:
        return datetime.fromtimestamp(SEEN.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def in_alert_window(now: datetime) -> bool:
    local = now.astimezone(TZ)
    return local.weekday() in WORKING_DAYS and WINDOW[0] <= local.hour < WINDOW[1]


def describe(seen: datetime | None, now: datetime) -> str:
    if seen is None:
        return "never, since the watchdog was installed"
    local = seen.astimezone(TZ).strftime("%d %b %Y at %H:%M")
    return f"{local} IST ({int((now - seen).total_seconds() // 60)} minutes ago)"


def down_body(seen: datetime | None, now: datetime) -> str:
    return f"""The Biomax terminal has stopped reaching the Avora bridge.

Last contact: {describe(seen, now)}

Attendance is NOT being recorded from the biometric device right now. The
dashboard will quietly fall back to laptop-agent times, which look plausible and
are not the formal record.

Nothing is lost yet. The terminal keeps its own log and the bridge re-reads it
on reconnect, so fixing the link recovers the missing punches automatically.

Most likely cause, in order:

  1. The office public IP changed and the firewall is now dropping the device.
     This has happened before. Check the current office IP against the rule:
         ufw status | grep 8090
     then allow the new address and the punches will flow again.
  2. The terminal lost power or its network settings were reset. It should point
     at ServerIP 65.21.186.55, ServerPort 8090.
  3. The bridge itself stopped:
         systemctl status avora-fk-bridge

-- Avora biometric watchdog
"""


def up_body(seen: datetime | None, now: datetime) -> str:
    return f"""The Biomax terminal is reaching the Avora bridge again.

Last contact: {describe(seen, now)}

Any punches made while the link was down are recovered from the terminal's
stored log on reconnect, so no action is needed.

-- Avora biometric watchdog
"""


def send_email(subject: str, body: str) -> None:
    """Mail through the same SMTP account the backend uses. Credentials arrive in
    the environment from systemd; nothing is written down and nothing is logged."""
    host, recipient = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL")
    if not host or not recipient:
        raise SystemExit("SMTP_HOST and ALERT_EMAIL are required")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("EMAIL_FROM") or recipient
    message["To"] = recipient
    message.set_content(body)

    port = int(os.environ.get("SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if (os.environ.get("SMTP_STARTTLS") or "true").lower() in {"1", "true", "yes"}:
            smtp.starttls(context=ssl.create_default_context())
        user, password = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD")
        if user and password:
            smtp.login(user, password)
        smtp.send_message(message)


def due_for_reminder(state: dict[str, str], now: datetime) -> bool:
    """True once the nag interval has passed, so a long outage keeps surfacing
    instead of scrolling out of an inbox after one message."""
    try:
        last = datetime.fromisoformat(state["notified_at"])
    except (KeyError, ValueError):
        return True
    return now - last >= timedelta(hours=REMINDER_HOURS)


def main() -> None:
    now = datetime.now(UTC)
    seen = last_contact()
    stale = seen is None or (now - seen).total_seconds() > STALE_SECONDS
    state = read_state()
    was_down = state.get("status") == "down"

    if seen is None and "status" not in state:
        # First run after an install: the bridge may have only just started and
        # the terminal polls on its own schedule. Note it and judge next pass,
        # rather than mailing an outage the moment the watchdog goes in.
        write_state({"status": "unknown"})
        print("no contact yet; first run, will judge on the next pass")
        return

    if stale and in_alert_window(now):
        if not was_down or due_for_reminder(state, now):
            send_email("Avora: biometric terminal is not reporting", down_body(seen, now))
            write_state({"status": "down", "notified_at": now.isoformat()})
            print(f"alerted: last contact {describe(seen, now)}")
            return
        print("still down; reminder not due yet")
        return

    if not stale and was_down:
        send_email("Avora: biometric terminal is back", up_body(seen, now))
        write_state({"status": "up", "notified_at": now.isoformat()})
        print("recovered; notified")
        return

    if not stale and state.get("status") != "up":
        write_state({"status": "up"})
    print(f"ok: last contact {describe(seen, now)}")


if __name__ == "__main__":
    sys.exit(main())
