"""Client for Circle's server-to-server endpoints (/api/internal/avora/*).

Reads ONE employee's pay, bank details and documents, looked up by work email.
Callers must authorize the person first: this client trusts its caller and is
only ever constructed behind a service that does (CircleImportService).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import NotFoundError, UpstreamUnavailableError
from app.core.logging import get_logger

logger = get_logger("app.circle")

SECRET_HEADER = "X-Avora-Secret"  # noqa: S105 (header name, not a secret)
_TIMEOUT_SECONDS = 15.0
# Circle document ids are short hex strings; anything else never reaches the URL.
_DOC_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class CircleFile:
    content: bytes
    content_type: str
    # True when the file is bigger than the caller's limit; `content` is then
    # empty - the rest was never downloaded.
    too_large: bool = False


def is_valid_doc_id(doc_id: str) -> bool:
    return bool(_DOC_ID.match(doc_id))


class CircleClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = settings.circle_api_url.rstrip("/")
        self._secret = settings.circle_api_secret
        self._transport = transport

    async def _get(self, path: str, email: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as http:
                resp = await http.get(
                    f"{self._base}/api/internal/avora{path}",
                    params={"email": email},
                    headers={SECRET_HEADER: self._secret},
                )
        except httpx.HTTPError as exc:
            logger.warning("circle_unreachable", extra={"error": type(exc).__name__})
            raise UpstreamUnavailableError() from exc
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == httpx.codes.NOT_FOUND:
            raise NotFoundError()
        if resp.status_code != httpx.codes.OK:
            logger.warning("circle_error", extra={"status": resp.status_code})
            raise UpstreamUnavailableError()

    async def _get_object(self, path: str, email: str) -> dict[str, Any]:
        """A JSON object, or UpstreamUnavailableError for anything else - a
        malformed reply is Circle failing, not a 500 in Avora."""
        resp = await self._get(path, email)
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamUnavailableError() from exc
        if not isinstance(body, dict):
            raise UpstreamUnavailableError()
        return body

    async def compensation(self, email: str) -> dict[str, Any]:
        return await self._get_object("/compensation", email)

    async def documents(self, email: str) -> list[dict[str, Any]]:
        listing = (await self._get_object("/documents", email)).get("documents")
        if not isinstance(listing, list):
            raise UpstreamUnavailableError()
        return [d for d in listing if isinstance(d, dict)]

    async def profile(self, email: str) -> dict[str, Any]:
        return await self._get_object("/profile", email)

    async def document_content(self, email: str, doc_id: str, *, max_bytes: int) -> CircleFile:
        if not is_valid_doc_id(doc_id):
            raise NotFoundError()
        return await self._download(f"/documents/{doc_id}/content", email, max_bytes=max_bytes)

    async def avatar_content(self, email: str, *, max_bytes: int) -> CircleFile:
        return await self._download("/avatar", email, max_bytes=max_bytes)

    async def _download(self, path: str, email: str, *, max_bytes: int) -> CircleFile:
        """Stream a file, stopping at `max_bytes`: an oversized file is never
        held in memory, only reported as `too_large`."""
        try:
            async with (
                httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as http,
                http.stream(
                    "GET",
                    f"{self._base}/api/internal/avora{path}",
                    params={"email": email},
                    headers={SECRET_HEADER: self._secret},
                ) as resp,
            ):
                self._raise_for_status(resp)
                content_type = resp.headers.get("content-type", "application/octet-stream")
                if int(resp.headers.get("content-length") or 0) > max_bytes:
                    return CircleFile(b"", content_type, too_large=True)
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return CircleFile(b"", content_type, too_large=True)
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            logger.warning("circle_unreachable", extra={"error": type(exc).__name__})
            raise UpstreamUnavailableError() from exc
        return CircleFile(b"".join(chunks), content_type)
