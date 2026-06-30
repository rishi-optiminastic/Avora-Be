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
  HEARTBEAT_URL_OCR     optional: liveness ping after a healthy cycle (Better Stack).
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import urllib.request
from typing import Any

import boto3
import psycopg2
import pytesseract
from PIL import Image, ImageOps

log = logging.getLogger("ocr_worker")

BATCH = int(os.getenv("OCR_BATCH", "3"))
IDLE_SLEEP = float(os.getenv("OCR_IDLE_SLEEP", "2"))
MAX_CHARS = int(os.getenv("OCR_MAX_CHARS", "20000"))
# Upscale each region (a single monitor, not the whole multi-monitor strip) so
# small code/UI text crosses Tesseract's legibility floor. Keyed off the region's
# longest side after cropping, so a wide-but-short ultrawide panel still gets help.
MIN_DIMENSION = int(os.getenv("OCR_MIN_DIMENSION", "2200"))
# LSTM engine (--oem 1), automatic page segmentation (--psm 3), keep word spacing.
TESS_CONFIG = os.getenv("OCR_TESSERACT_CONFIG", "--oem 1 --psm 3 -c preserve_interword_spaces=1")
# Pillow ≥9.1 moved resampling filters under Image.Resampling; fall back for older.
_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
# Liveness heartbeat — this loop spins every ~2s, so throttle the ping. Self-
# contained (the OCR image bundles only this file), so it's inlined, not shared
# with worker/heartbeat.py. Pinged only on a healthy cycle, so a DB outage alerts.
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL_OCR", "").strip()
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "60"))

_s3: Any = None


def _heartbeat(last_beat: float) -> float:
    """Best-effort liveness ping, throttled to HEARTBEAT_INTERVAL. Returns the new
    last-beat time. Never raises — monitoring must not take the worker down."""
    if not HEARTBEAT_URL:
        return last_beat
    now = time.monotonic()
    if now - last_beat < HEARTBEAT_INTERVAL:
        return last_beat
    try:
        with urllib.request.urlopen(HEARTBEAT_URL, timeout=5) as resp:  # noqa: S310
            resp.read(64)
    except Exception as exc:  # never let monitoring break the worker
        log.warning("heartbeat ping failed: %s", exc)
    return now


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


def _coerce_monitors(value: Any) -> list[list[int]] | None:
    """Per-monitor rects [[x,y,w,h], …] from the JSON column (psycopg2 usually hands
    back a list already; tolerate a raw string too). None ⇒ OCR the whole image."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) and value else None


def _regions(img: Image.Image, monitors: list[list[int]] | None) -> list[Image.Image]:
    """Split the combined capture into one crop per monitor (clamped to the image
    bounds; slivers ignored), or the whole image when there's no usable metadata.
    Per-monitor crops let Tesseract segment one coherent layout at a time instead of
    two unrelated side-by-side desktops, which it handles much better."""
    if monitors:
        crops: list[Image.Image] = []
        for rect in monitors:
            if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
                continue
            try:
                x, y, w, h = (int(v) for v in rect)
            except (TypeError, ValueError):
                continue
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(img.width, x + w), min(img.height, y + h)
            if x1 - x0 >= 200 and y1 - y0 >= 120:  # ignore slivers / bad rects
                crops.append(img.crop((x0, y0, x1, y1)))
        if crops:
            return crops
    return [img]


def _preprocess(region: Image.Image) -> Image.Image:
    """Grayscale + contrast-stretch + upscale ONE region so Tesseract reads small
    text. Upscaling keys off the region's longest side (post-crop), so a single
    monitor is enlarged even when the combined strip was already wide."""
    out = ImageOps.autocontrast(region.convert("L"), cutoff=1)
    longest = max(out.size)
    if longest and longest < MIN_DIMENSION:
        scale = MIN_DIMENSION / longest
        out = out.resize((round(out.width * scale), round(out.height * scale)), _LANCZOS)
    return out


def _ocr(image: bytes, monitors: list[list[int]] | None) -> str:
    """OCR each monitor region and join with blank lines so per-screen context stays
    grouped. Whitespace within a region is collapsed; the whole result is capped."""
    base = ImageOps.exif_transpose(Image.open(io.BytesIO(image)))
    chunks: list[str] = []
    for region in _regions(base, monitors):
        text = pytesseract.image_to_string(_preprocess(region), config=TESS_CONFIG)
        cleaned = " ".join(text.split())
        if cleaned:
            chunks.append(cleaned)
    return "\n".join(chunks)[:MAX_CHARS]


def _process_batch(conn: psycopg2.extensions.connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, object_key, image, monitors FROM screenshots "
            "WHERE ocr_status = 'PENDING' ORDER BY received_at ASC LIMIT %s",
            (BATCH,),
        )
        rows = cur.fetchall()
        for row_id, object_key, image, monitors in rows:
            try:
                text = _ocr(_load_image(object_key, image), _coerce_monitors(monitors))
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
    last_beat = 0.0
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
            last_beat = _heartbeat(last_beat)  # healthy cycle → report liveness
        except psycopg2.Error as exc:
            log.warning("db error, reconnecting: %s", exc)
            conn = None
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    main()
