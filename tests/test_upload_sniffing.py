"""Uploaded files are identified by their CONTENT, not by what the client says.

The browser's Content-Type is wrong in both directions: real PDFs often arrive as
application/octet-stream (so header-trusting rejected genuine receipts), and any
file at all can claim to be a PDF (so header-trusting also let scripts through).
"""

from __future__ import annotations

from app.core.uploads import detect_media_type

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
WEBP = b"RIFF" + (64).to_bytes(4, "little") + b"WEBP" + b"0" * 32


def test_the_real_formats_are_recognised_from_their_bytes() -> None:
    assert detect_media_type(PDF) == "application/pdf"
    assert detect_media_type(PNG) == "image/png"
    assert detect_media_type(JPEG) == "image/jpeg"
    assert detect_media_type(WEBP) == "image/webp"


def test_anything_else_is_rejected() -> None:
    assert detect_media_type(b"<script>alert(1)</script>") is None
    assert detect_media_type(b"MZ\x90\x00") is None  # a Windows executable
    assert detect_media_type(b"") is None
    assert detect_media_type(b"%PD") is None  # a truncated signature is not a PDF
    # "RIFF" alone is a WAV or an AVI, not a WebP image.
    assert detect_media_type(b"RIFF" + (64).to_bytes(4, "little") + b"WAVE") is None
