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
        if resp.status_code == httpx.codes.NOT_FOUND:
            raise NotFoundError()
        if resp.status_code != httpx.codes.OK:
            logger.warning("circle_error", extra={"status": resp.status_code})
            raise UpstreamUnavailableError()
        return resp

    async def compensation(self, email: str) -> dict[str, Any]:
        body: dict[str, Any] = (await self._get("/compensation", email)).json()
        return body

    async def documents(self, email: str) -> list[dict[str, Any]]:
        body = (await self._get("/documents", email)).json()
        return list(body.get("documents") or [])

    async def document_content(self, email: str, doc_id: str) -> CircleFile:
        if not _DOC_ID.match(doc_id):
            raise NotFoundError()
        resp = await self._get(f"/documents/{doc_id}/content", email)
        return CircleFile(
            resp.content, resp.headers.get("content-type", "application/octet-stream")
        )
