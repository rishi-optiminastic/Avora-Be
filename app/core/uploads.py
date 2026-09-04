"""Identify an uploaded file from its CONTENT, not from what the client called it.

The browser-supplied `Content-Type` is a claim, not a fact (Golden rule #1), and
it is wrong in both directions. A genuine PDF frequently arrives as
`application/octet-stream` — some OS/browser combinations simply do not fill the
field in — so trusting the header rejects real receipts. And anything at all can
*say* it is `application/pdf`, so trusting the header also lets a script through.

Sniffing the leading magic bytes settles both: the file is what it is.
"""

from __future__ import annotations

from collections.abc import Callable

# Longest signature we inspect (WebP needs 12 bytes: "RIFF" + size + "WEBP").
_HEADER_BYTES = 12


def _is_webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


# Ordered so a cheap prefix test comes before the callable ones.
_SIGNATURES: tuple[tuple[str, bytes | Callable[[bytes], bool]], ...] = (
    ("application/pdf", b"%PDF-"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/webp", _is_webp),
)


def detect_media_type(data: bytes) -> str | None:
    """The real media type of `data`, or None if it is not one we accept.

    Only the formats a receipt or proof can legitimately be. Returning None is a
    rejection, so a new format has to be added here deliberately rather than
    slipping in behind a content-type header.
    """
    head = data[:_HEADER_BYTES]
    for media_type, signature in _SIGNATURES:
        if callable(signature):
            if signature(head):
                return media_type
        elif head.startswith(signature):
            return media_type
    return None
