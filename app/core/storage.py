"""S3 object storage for screenshot images.

Screenshots are large binary blobs; keeping them in Postgres bloats the DB and
makes reads slow. Instead we upload the image to S3 under an opaque key and store
only that key on the row. Reads hand the browser a short-lived presigned URL so
the bytes never stream through the API again.

Credentials and bucket come from Settings (env) only — never hardcoded (rule
5.6). When S3 is not configured (`settings.s3_enabled` is False) callers fall
back to storing bytes in the DB, so local/test runs work with no AWS at all.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import boto3

from app.core.config import get_settings

_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


@lru_cache
def _client() -> Any:
    """Lazy, cached S3 client. Uses explicit keys when provided, otherwise the
    default credential chain (IAM role / shared config)."""
    s = get_settings()
    kwargs: dict[str, str] = {"region_name": s.aws_region}
    if s.aws_access_key_id and s.aws_secret_access_key:
        kwargs["aws_access_key_id"] = s.aws_access_key_id
        kwargs["aws_secret_access_key"] = s.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def object_key(employee_id: str, screenshot_id: str, content_type: str) -> str:
    """Build an opaque, collision-free key partitioned by employee."""
    prefix = get_settings().s3_key_prefix.strip("/")
    ext = _EXT_BY_TYPE.get(content_type, "bin")
    return f"{prefix}/{employee_id}/{screenshot_id}.{ext}"


async def put_object(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes to the configured bucket (boto3 is sync → off-thread)."""
    s = get_settings()

    def _put() -> None:
        _client().put_object(
            Bucket=s.aws_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    await asyncio.to_thread(_put)


async def delete_objects(keys: list[str]) -> None:
    """Delete objects (used by retention so purged rows don't orphan their S3
    blobs). Batches in groups of 1000 (the S3 delete_objects limit)."""
    if not keys:
        return
    s = get_settings()

    def _delete(batch: list[str]) -> None:
        _client().delete_objects(
            Bucket=s.aws_bucket_name,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )

    for i in range(0, len(keys), 1000):
        await asyncio.to_thread(_delete, keys[i : i + 1000])


def presigned_get_url(key: str) -> str:
    """A short-lived URL the browser can GET the image from directly. Signing is
    a local operation (no network), so this is cheap to call per request."""
    s = get_settings()
    url: str = _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": s.aws_bucket_name, "Key": key},
        ExpiresIn=s.s3_url_ttl_seconds,
    )
    return url
