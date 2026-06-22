# Deploying the background workers on Render

The schedulers (`worker/eod_scheduler.py`, `worker/payroll_scheduler.py`) run as
Render **Background Workers** — long-running processes with no HTTP port. They
reuse the API's Docker image and just override the start command, so there's
nothing extra to build.

Defined as code in [`render.yaml`](../render.yaml).

## One-time setup

1. **Render → Blueprints → New Blueprint Instance** and pick the `be` repo.
   Render reads `render.yaml` and proposes:
   - `avora-eod-scheduler` — `python -m worker.eod_scheduler`
   - `avora-payroll-scheduler` — `python -m worker.payroll_scheduler`
   - an env group `avora-workers` shared by both.
2. **Fill the secret env vars** it prompts for (`sync: false` keys):
   - `DATABASE_URL` — the asyncpg URL of the prod Postgres (same DB the API uses).
   - `SENDGRID_API_KEY`, `EMAIL_FROM` — so reports/digests can be delivered.
   - `OPENROUTER_API_KEY`, `EOD_MODEL` — the LLM for EOD (e.g.
     `anthropic/claude-sonnet-4.5`).
   The non-secret defaults (hours, ticks, `EOD_ENABLED=true`) come from the file.
3. **Apply.** Each worker boots, runs its loop, and logs to the Render dashboard.

## Migrations (do this before/at release — not in the worker)

Workers assume the schema is current. Run migrations from your **API** deploy or
a one-off **Render Job**, never from the workers (multi-instance race —
CLAUDE.md §8):

```
alembic upgrade head
```

For EOD specifically the new table is `eod_reports` (migration `a1b2c3d4e5f6`).

## Turning EOD on / off

- The blueprint sets `EOD_ENABLED=true`. To pause generation without redeploying,
  set `EOD_ENABLED=false` in the `avora-workers` env group and restart the worker.
- `EOD_REPORT_HOUR` is the local hour (org attendance-policy timezone) at which
  drafts are generated; `EOD_AUTO_SEND_AFTER_HOURS` is the unreviewed-draft cutoff.

## Notes

- **Region:** both workers are pinned to `singapore` to sit next to the Postgres
  (ap-southeast-1) for low round-trip latency.
- **Plan:** `starter` — workers can't use Render's free tier (free is web-only and
  spins down). Bump the plan if generation volume grows.
- **OCR worker** (`worker/ocr_worker.py`) is **not** on Render — it needs Tesseract
  and runs on its own VPS (see its module docstring).
