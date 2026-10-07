# Avora OCR worker

Runs **separately** from the API (e.g. on a private Hetzner VPS). It pulls
`PENDING` screenshots from Postgres, OCRs them with Tesseract, and writes
`ocr_text` + sets `ocr_status = DONE` (or `FAILED`). That text feeds work
attribution. Self-contained — no FastAPI/app dependency.

Screenshot images now live in **S3**; rows carry an `object_key` and the worker
downloads the image from S3 before OCR. (Pre-S3 rows with in-DB `image` bytes
still work as a fallback.)

## Run with Docker
```bash
cd be/worker
docker build -t avora-ocr-worker .
docker run -d --name avora-ocr --restart unless-stopped \
  -e DATABASE_URL="postgresql://USER:PASS@HOST/DB?sslmode=require" \
  -e AWS_REGION="ap-south-1" \
  -e AWS_BUCKET_NAME="your-bucket" \
  -e AWS_ACCESS_KEY_ID="..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  avora-ocr-worker
```
The API's `DATABASE_URL` (asyncpg style, `...+asyncpg://...?ssl=require`) is
accepted too — it's normalised to a psycopg DSN automatically.

## S3 (env)
- `AWS_REGION` — bucket region (e.g. `ap-south-1`).
- `AWS_BUCKET_NAME` — bucket holding screenshot images.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — optional; omit to use the
  instance/default credential chain.

## Tunables (env)
- `OCR_BATCH` — screenshots per cycle (default 3); raise to use more cores.
- `OCR_IDLE_SLEEP` — seconds to wait when the queue is empty (default 2).
- `OCR_MAX_CHARS` — cap stored text length (default 20000).

## Security
Give it a **least-privilege DB role** that can only `SELECT` the `screenshots`
table and `UPDATE` its `ocr_text`/`ocr_status` columns. The S3 IAM principal
needs only `s3:GetObject` on the screenshots prefix.

---

# Task-escalation scheduler

`worker/task_escalation_scheduler.py` makes an overdue task progressively louder
instead of waiting for someone to notice it:

| Overdue by | What happens |
|---|---|
| `TASK_ESCALATION_WARN_AFTER_DAYS` (1) | the assignee gets a warning that pops up on the dashboard |
| `TASK_ESCALATION_MANAGER_AFTER_DAYS` (3) | the reporting manager is added as a **collaborator** and notified |
| `TASK_ESCALATION_ADMIN_AFTER_DAYS` (6) | an admin is added as a **collaborator** and notified |

Adding a collaborator is the point: it puts the task in that person's scope so
they can actually see and comment on it, rather than just being told about it.

Each tier fires **exactly once** per task, tracked by `tasks.escalation_level`,
so the sweep is idempotent - ticking hourly forever, or re-running after a crash,
never re-notifies or re-adds anyone. Completing a task stops it escalating.

```bash
docker run -d --name avora-task-escalation --restart unless-stopped \
  -e DATABASE_URL="postgresql+asyncpg://USER:PASS@HOST/DB" \
  -e TASK_ESCALATION_ENABLED=true \
  avora-api python -m worker.task_escalation_scheduler
```

Run **one instance only** - two would race on the same tasks.

## Tunables (env)
- `TASK_ESCALATION_ENABLED` - `true` to turn it on (default **off**: it adds
  people to tasks, so switch it on deliberately).
- `TASK_ESCALATION_TICK_SECONDS` - seconds between sweeps (default 3600).
- `TASK_ESCALATION_WARN_AFTER_DAYS` / `_MANAGER_AFTER_DAYS` / `_ADMIN_AFTER_DAYS`
  - the three thresholds above (defaults 1 / 3 / 6).

Requires migration `f1a3c5e7b9d2` (adds `tasks.escalation_level`).
