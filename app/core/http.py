"""HTTP request helpers.

`read_capped_body` buffers a request body but refuses to read past a byte cap, so
a hostile client can't OOM the server by streaming a multi-GB upload. It rejects
early on an oversized `Content-Length`, then streams and aborts the moment the
running total exceeds the cap — which also catches a lying/absent Content-Length
(e.g. chunked transfer).
"""

from __future__ import annotations

from fastapi import Request

from app.core.exceptions import PayloadTooLargeError


async def read_capped_body(request: Request, max_bytes: int) -> bytes:
    # Cheap pre-check: trust-but-verify the declared length.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise PayloadTooLargeError()
        except ValueError:
            pass  # malformed header — fall through to the streamed cap

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)
