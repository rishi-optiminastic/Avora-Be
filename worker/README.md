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
