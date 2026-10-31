# FK bridge: the biometric terminal talks to Avora directly

`fk_bridge.py` replaces the office PC.

The Biomax N-BM30W terminal pushes every punch straight to a small always-on
server, which signs it and forwards it to Avora.
No Windows box, no vendor middleware, nothing in the office that can be switched
off.

## Why this exists

The old path was `terminal -> office PC (Smart Office + connector) -> Avora`.
That PC died, and with it went the only thing listening to the terminal.
Punches kept accumulating in the terminal's own memory, but nothing moved them,
and Zoho has a permanent gap for 26 to 29 October 2026 as a result.

The fix is to delete the fragile hop.
The terminal is perfectly capable of posting to a server on the internet, so it
now posts to ours.

```
[Biomax terminal] --FK/EBKN POST--> [fk_bridge on the VPS] --HMAC POST--> [Avora]
   office                             65.21.186.55:8090        /api/v1/attendance/biometric
```

## Why it is not behind Caddy

The FK protocol carries everything that matters in headers named
`request_code`, `dev_id`, `blk_no` and `cmd_id`.
Nginx and Caddy both drop headers containing underscores, so a reverse proxy
silently destroys the protocol.
The bridge therefore listens on its own port and is reached directly.

That also means there is no TLS on this hop and no authentication beyond the
device id, which is printed on the terminal's screen.
**The firewall rule is the actual access control**, which is why the port is
opened to the office IP only and not to the world.

## How it cannot quietly lose punches

Three independent layers, in order of how quickly they act.

1. **Acknowledgement.**
   The terminal only considers a record delivered when it gets back HTTP 200,
   an empty body and a `response_code: OK` header.
   Anything else and it keeps the record and re-sends it.
2. **The spool.**
   If Avora is unreachable, the punch is written to `fk-spool.jsonl` and goes
   out on the back of the next successful send.
   Nothing is dropped because the API had a bad minute.
3. **The catch-up.**
   Every six hours the bridge asks the terminal for its entire stored log and
   forwards only what is newer than the high-water mark in
   `fk-bridge-state.json`.
   A punch has to escape the live push *and* the catch-up to be lost.

The terminal stores years of history, so the catch-up is also the repair tool:
if the bridge is down for a day, the next catch-up fills the day in by itself.

### First run is deliberately quiet

On a machine with no `fk-bridge-state.json`, the first log dump only records
where we are and sends nothing.
That dump goes back to January 2022.
Replaying it would rewrite attendance, and therefore payroll, for months that
are long closed.
History is a deliberate act: use `backfill.py` with an explicit date range.

## What is deployed

On the VPS, `65.21.186.55`:

| Thing | Path |
| --- | --- |
| Script | `/opt/avora/fk-bridge/fk_bridge.py` |
| Launcher | `/opt/avora/fk-bridge/run.sh` |
| Service | `/etc/systemd/system/avora-fk-bridge.service` |
| Spool, state, log | `/opt/avora/fk-bridge/fk-{spool.jsonl,bridge-state.json,bridge.log}` |

The service is enabled at boot, has `Restart=always`, runs with no capabilities
and a read-only filesystem apart from its own directory, and logs in
`Asia/Kolkata` so log lines and punch times can be compared by eye.

### The secret

There is no second copy of `BIOMETRIC_WEBHOOK_SECRET` on the box.
systemd reads the API application's own Coolify env file as PID 1, before the
sandbox applies, and exposes it to this service alone as a credential in tmpfs.
`run.sh` lifts out that one key and leaves the other seventy-five behind.

Two consequences:

- Rotating the secret in Coolify takes effect on `systemctl restart avora-fk-bridge`.
- The bridge needs no capability to read the Coolify directory, which a process
  on a public port should not have.

## Operating it

```bash
systemctl status avora-fk-bridge            # is it up
journalctl -u avora-fk-bridge -f            # follow it live
journalctl -u avora-fk-bridge --since today # today's punches
systemctl restart avora-fk-bridge           # after a secret rotation
```

Drain a backlog without waiting for someone to touch the sensor:

```bash
systemctl stop avora-fk-bridge
systemd-run --quiet --wait --pipe --collect \
  -p WorkingDirectory=/opt/avora/fk-bridge \
  -p LoadCredential=api-env:/data/coolify/applications/cxociqwdpyq3yxrt9uezicoe/.env \
  /opt/avora/fk-bridge/run.sh --flush
systemctl start avora-fk-bridge
```

Stopping first matters: both processes write the same spool file.

## Terminal settings

On the device, `Menu -> Comm -> Network` (labels vary by firmware):

| Setting | Value |
| --- | --- |
| `ServerIP` | `65.21.186.55` |
| `ServerPort` | `8090` |

## Known gaps

- **Device user `126` matches nobody in Avora.**
  Its punches are accepted and reported as unmatched, so they are visible in the
  log but produce no attendance.
  Set that person's Biometric ID in Avora, then re-run `backfill.py` for the
  affected dates.
  It is idempotent.
- **The office IP is probably dynamic.**
  The firewall rule names `103.167.176.56`.
  If the office IP changes, the terminal is silently refused and punches sit in
  its memory until the rule is updated, at which point the catch-up recovers
  them.
  Worth checking the rule first whenever punches stop arriving.
- **Three active employees have no Biometric ID at all**, so they can never be
  matched.
