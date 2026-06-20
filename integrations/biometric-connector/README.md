# Avora biometric connector

Pushes attendance punches from your office biometric setup into Avora, so the PMS
tracks attendance from the **same device that already feeds Zoho**. It runs on the
office PC (the one that talks to the device), signs each batch with HMAC, and POSTs
to Avora's `POST /api/v1/attendance/biometric`.

Avora turns punches into the formal attendance record (`source="biometric"`) and
**reconciles** them against laptop-agent activity (see Time → Reconciliation),
flagging mismatches — punched-but-not-active, active-but-no-punch, or login/logout
times that disagree.

## How it fits

```
[Biometric device] ──> [Office PC] ──HMAC POST──> [Avora backend] ──> attendance + reconciliation
                         this connector            /attendance/biometric
                              │
                              └─ also still pushes to Zoho (unchanged)
```

## What to find out first

On the office PC, note the vendor software (eSSL eTimeTrackLite, ZKTeco BioTime,
Matrix, …). Then pick a source:

- **CSV** (works with any vendor): have the middleware export a punch report to a
  CSV and point `CSV_PATH` at it. Header: `external_id,punched_at`.
- **ZK** (ZKTeco/eSSL family): the connector pulls directly from the device over
  the LAN via `pyzk`. Set `ZK_HOST`/`ZK_PORT`.

## Employee mapping

Each punch carries an `external_id`. Avora resolves it to an employee by, in order:
**biometric_id** → **hr_external_id** → **work_email**. Easiest is to send the
person's **work email** as `external_id` (CSV column, or a `MAPPING_CSV` for ZK).
Otherwise set each employee's `biometric_id` (their device enrolment number) in
Avora — via the HR sync (`biometric_id` field) or an admin. Unmatched ids are
reported in the response, never silently dropped.

## Setup

```bash
cp config.example.env .env          # fill in AVORA_API_URL + BIOMETRIC_WEBHOOK_SECRET
pip install -r requirements.txt     # only needed for SOURCE=zk
python connector.py                 # one pass
python connector.py --loop 300      # poll every 5 minutes
```

`BIOMETRIC_WEBHOOK_SECRET` must equal the backend's. On the server set
`BIOMETRIC_WEBHOOK_SECRET` (and optionally `BIOMETRIC_IP_ALLOWLIST` with the
office's public IP/CIDR).

## Running unattended

- **Windows**: Task Scheduler → run `python connector.py` every few minutes (or
  once with `--loop`), "Run whether user is logged on or not".
- **Linux**: a cron entry, or a systemd service running `--loop 300`.

The connector keeps a watermark (`STATE_FILE`) of the last punch sent, so it only
pushes new ones. Re-sending is safe anyway — the server merges punches into one
session per employee per day (idempotent).
