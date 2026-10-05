# Avora biometric connector

Pushes attendance punches from your office biometric setup into Avora, so the PMS
tracks attendance from the **same device that already feeds Zoho**. It runs on the
office PC (the one that talks to the device), signs each batch with HMAC, and POSTs
to Avora's `POST /api/v1/attendance/biometric`.

Avora turns punches into the formal attendance record (`source="biometric"`) and
**reconciles** them against laptop-agent activity (see Time -> Reconciliation),
flagging mismatches - punched-but-not-active, active-but-no-punch, or login/logout
times that disagree.

## How it fits

```
[Biometric device] -> [Office PC] --HMAC POST--> [Avora backend] -> attendance + reconciliation
                        this connector            /attendance/biometric
                             |
                             +- also still pushes to Zoho (unchanged)
```

## Install on the office PC (Windows)

1. Copy this folder to the PC, e.g. `C:\Avora\biometric-connector`.
2. Copy `avora-config.example.json` to `avora-config.json` and paste the real
   `BIOMETRIC_WEBHOOK_SECRET` (it must equal the backend's).
3. Open PowerShell **as the user who owns the Smart Office database** and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -KeepAwake
```

That checks every prerequisite first, registers a self-healing scheduled task, runs
one sync immediately, and prints whether it worked. `-KeepAwake` also stops the PC
sleeping, because an asleep PC sends nothing.

### Day-to-day

| I want to... | Do this |
|---|---|
| Check it is still working | double-click `check-health.bat` |
| Find out why it broke | double-click `diagnose.bat` |
| Force a sync right now | double-click `run_avora.bat` |
| Remove it | `install-windows.ps1 -Uninstall` |

## Why it runs the way it does

The connector runs as a **short one-shot every 5 minutes**, not as a long-lived
`--loop` daemon. That is deliberate, and it is the fix for "the task was set up but
attendance stopped arriving":

- Task Scheduler's default **`ExecutionTimeLimit` is 3 days**. It silently kills a
  `--loop` process, and an "At startup" trigger does not start it again until the
  next reboot. The task still *looks* installed.
- A crash, a SQL Server restart, a network drop or a reboot each ended a daemon
  permanently. A one-shot just fails that tick; the next one five minutes later
  retries on its own.
- A one-shot cannot be killed for running too long and cannot leak memory.

Re-running is always safe: the connector keeps a watermark of the last punch it
sent, and the Avora server merges punches into one session per employee per day.

The task is registered with: `AtStartup` + `AtLogOn` + a 5-minute repetition,
`StartWhenAvailable` (so a tick missed while the PC was off is caught up), S4U
logon (runs with nobody logged on, no stored password), restart-on-failure, and
`IgnoreNew` so ticks never overlap.

## If attendance stops arriving

Run `diagnose.bat`. It checks, and names the one that is broken:

- the webhook secret is configured
- `pyodbc` and a SQL Server ODBC driver are present
- `SmartOfficedb` is readable **as the account the task runs as**
- the Avora backend is reachable

The most common cause after a working install is the third one: the task was
registered under an account that has no rights on the Smart Office database, so
every run fails at the same point. Re-run `install-windows.ps1` from an account
that can open the database.

`check-health.bat` reports the last successful sync and exits non-zero if it is
more than 30 minutes old, so it can also be wired into any monitoring you have.

Logs are in `avora_biometric.log`, rotated at 2 MB (3 kept), so they cannot fill
the disk.

## Employee mapping

Each punch carries an `external_id` - the device's UserId. Avora resolves it to an
employee by **biometric_id** -> **hr_external_id** -> **work_email**. Set each
person's **Biometric ID** in Avora (Admin -> profile), or send their work email as
the id. Unmatched ids are reported in the response and logged as a warning, never
silently dropped - so a new joiner whose Biometric ID was never set shows up in the
log rather than just going missing from attendance.

## Rotating the webhook secret

The secret used to be hardcoded in `avora_biometric.py` and is therefore **in git
history**. Treat it as compromised and rotate it:

1. Set a new `BIOMETRIC_WEBHOOK_SECRET` on the backend and redeploy.
2. Put the same value in `avora-config.json` on the office PC.
3. Run `check-health.bat` to confirm the next sync succeeds.

`avora-config.json` is gitignored. Never put the secret back in the `.py` file.

Optionally set `BIOMETRIC_IP_ALLOWLIST` on the backend to the office's public
IP/CIDR, so a leaked secret alone is not enough to post punches.

## Other sources (non-Smart-Office sites)

`connector.py` is the generic variant for CSV exports or direct ZKTeco/eSSL
(`pyzk`) pulls. Same endpoint, same HMAC, same mapping rules. See
`config.example.env`.

## Linux

```bash
*/5 * * * * cd /opt/avora-biometric && /usr/bin/python3 avora_biometric.py --quiet
```

Same reasoning: a repeating one-shot, not a daemon.
