"""Avora OCR worker — runs on a separate (Hetzner) VPS, not the API host.

Continuously pulls PENDING screenshots from Postgres, runs Tesseract OCR, and
writes the extracted text back. Self-contained: talks to the DB with raw SQL
(psycopg2), so it does NOT import the FastAPI app and stays a tiny image.

Env:
  DATABASE_URL          Postgres URL (asyncpg-style is normalised to psycopg).
  AWS_REGION            S3 region (e.g. ap-south-1) — required for S3-backed rows.
  AWS_BUCKET_NAME       S3 bucket holding screenshot images.
  AWS_ACCESS_KEY_ID     (optional) explicit creds; else the default chain.
  AWS_SECRET_ACCESS_KEY (optional) explicit creds; else the default chain.
  OCR_BATCH             rows per cycle (default 3) — raise to use more CPU cores.
  OCR_IDLE_SLEEP        seconds to sleep when the queue is empty (default 2).
  OCR_MAX_CHARS         cap stored text length (default 20000).
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any

import boto3
import psycopg2
import pytesseract
from PIL import Image, ImageOps

log = logging.getLogger("ocr_worker")

BATCH = int(os.getenv("OCR_BATCH", "3"))
IDLE_SLEEP = float(os.getenv("OCR_IDLE_SLEEP", "2"))
MAX_CHARS = int(os.getenv("OCR_MAX_CHARS", "20000"))
MIN_DIMENSION = 1600  # upscale smaller captures so text is legible to Tesseract

_s3: Any = None


def _s3_client() -> Any:
    """Lazy, cached S3 client (creds from env or the default chain)."""
    global _s3
    if _s3 is None:
        kwargs: dict[str, str] = {}
        if os.getenv("AWS_REGION"):
            kwargs["region_name"] = os.environ["AWS_REGION"]
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
            kwargs["aws_access_key_id"] = os.environ["AWS_ACCESS_KEY_ID"]
            kwargs["aws_secret_access_key"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        _s3 = boto3.client("s3", **kwargs)
    return _s3


def _load_image(object_key: str | None, image: bytes | memoryview | None) -> bytes:
    """Fetch the image bytes from S3 (preferred) or the legacy in-DB column."""
    if object_key:
        bucket = os.environ["AWS_BUCKET_NAME"]
        obj = _s3_client().get_object(Bucket=bucket, Key=object_key)
        data: bytes = obj["Body"].read()
        return data
    if image is not None:
        return bytes(image)
    raise ValueError("screenshot has neither object_key nor image bytes")


def _dsn() -> str:
    """Normalise an asyncpg-style URL (used by the API) to a psycopg2 DSN."""
    url = os.environ["DATABASE_URL"]
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgres+asyncpg://", "postgresql://")
    # asyncpg spells TLS `ssl=require`; psycopg/libpq spells it `sslmode=require`.
    return url.replace("ssl=require", "sslmode=require")


def _preprocess(data: bytes) -> Image.Image:
    """Grayscale + upscale to improve OCR accuracy on UI text."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
    longest = max(img.size)
    if longest and longest < MIN_DIMENSION:
        scale = MIN_DIMENSION / longest
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    return img


def _ocr(image: bytes) -> str:
    text = pytesseract.image_to_string(_preprocess(image))
    return " ".join(text.split())[:MAX_CHARS]


def _process_batch(conn: psycopg2.extensions.connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, object_key, image FROM screenshots "
            "WHERE ocr_status = 'PENDING' ORDER BY received_at ASC LIMIT %s",
            (BATCH,),
        )
        rows = cur.fetchall()
        for row_id, object_key, image in rows:
            try:
                text = _ocr(_load_image(object_key, image))
                cur.execute(
                    "UPDATE screenshots SET ocr_text = %s, ocr_status = 'DONE' WHERE id = %s",
                    (text, row_id),
                )
            except Exception as exc:
                log.warning("OCR failed for %s: %s", row_id, exc)
                cur.execute("UPDATE screenshots SET ocr_status = 'FAILED' WHERE id = %s", (row_id,))
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Avora OCR worker starting (batch=%d)", BATCH)
    conn: psycopg2.extensions.connection | None = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(_dsn())
                conn.autocommit = True
                log.info("connected to Postgres")
            processed = _process_batch(conn)
            if processed:
                log.info("processed %d screenshot(s)", processed)
            else:
                time.sleep(IDLE_SLEEP)
        except psycopg2.Error as exc:
            log.warning("db error, reconnecting: %s", exc)
            conn = None
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    main()
