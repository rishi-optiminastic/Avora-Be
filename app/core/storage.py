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
from botocore.config import Config

from app.core.config import get_settings

_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


@lru_cache
def _client() -> Any:
    """Lazy, cached S3 client. Uses explicit keys when provided, otherwise the
    default credential chain (IAM role / shared config).

    Forces SigV4 + virtual-hosted addressing so presigned URLs use the *regional*
    endpoint (`bucket.s3.<region>.amazonaws.com`). Without this, boto3 signs
    against the global `s3.amazonaws.com` endpoint and S3 rejects the presigned
    GET with 403 for buckets outside us-east-1 (e.g. ap-south-1)."""
    s = get_settings()
    kwargs: dict[str, Any] = {
        "region_name": s.aws_region,
        "config": Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    }
    if s.aws_access_key_id and s.aws_secret_access_key:
        kwargs["aws_access_key_id"] = s.aws_access_key_id
        kwargs["aws_secret_access_key"] = s.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def object_key(employee_id: str, screenshot_id: str, content_type: str) -> str:
    """Build an opaque, collision-free key partitioned by employee."""
    prefix = get_settings().s3_key_prefix.strip("/")
    ext = _EXT_BY_TYPE.get(content_type, "bin")
    return f"{prefix}/{employee_id}/{screenshot_id}.{ext}"


def avatar_object_key(employee_id: str, content_type: str) -> str:
    """Opaque key for a profile photo, under the IAM-allowed workspace prefix.
    One key per employee (kept stable-ish by id + a short suffix so a re-upload
    doesn't collide in caches)."""
    prefix = get_settings().s3_workspace_prefix.strip("/")
    ext = _EXT_BY_TYPE.get(content_type, "bin")
    return f"{prefix}/avatars/{employee_id}.{ext}"


def logo_object_key(content_type: str) -> str:
    """Opaque key for the workspace logo (a singleton), under the workspace
    prefix. One stable key per content type so a re-upload overwrites in place."""
    prefix = get_settings().s3_workspace_prefix.strip("/")
    ext = _EXT_BY_TYPE.get(content_type, "bin")
    return f"{prefix}/branding/logo.{ext}"


def workspace_object_key(file_id: str, filename: str | None) -> str:
    """Opaque key for a workspace file blob, preserving a safe extension so the
    object is recognizable in the bucket (the row is the source of truth)."""
    prefix = get_settings().s3_workspace_prefix.strip("/")
    ext = ""
    if filename and "." in filename:
        candidate = filename.rsplit(".", 1)[1].lower()
        if candidate.isalnum() and len(candidate) <= 8:
            ext = f".{candidate}"
    return f"{prefix}/{file_id}{ext}"


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


def presigned_get_url(key: str, *, download_filename: str | None = None) -> str:
    """A short-lived URL the browser can GET the object from directly. Signing is
    a local operation (no network), so this is cheap to call per request.

    When `download_filename` is given, the URL forces a download by overriding the
    response with `Content-Disposition: attachment` and a neutral content type — so
    a user-uploaded `*.html`/`svg` can never be served INLINE and run script, even
    if the presigned URL leaks. Leave it None for objects meant to render inline
    (e.g. screenshots in the monitor)."""
    s = get_settings()
    params: dict[str, str | int] = {"Bucket": s.aws_bucket_name, "Key": key}
    if download_filename is not None:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'
        params["ResponseContentType"] = "application/octet-stream"
    url: str = _client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=s.s3_url_ttl_seconds,
    )
    return url
