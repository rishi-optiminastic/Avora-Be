"""Shared font registration for the reportlab documents (payslips, letters).

Built-in Helvetica only covers Latin-1, so a name with any other script, or the
rupee sign, renders as tofu. We register the bundled DejaVu (or a system
equivalent) once, under one family name, and fall back to Helvetica when none is
present so a PDF always renders on a bare container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_FAMILY = "AvoraSans"
_BUNDLED = os.path.join(os.path.dirname(__file__), "fonts")

# The bundled DejaVu is tried first - it definitely has the rupee sign (U+20B9);
# many system "Unicode" fonts (e.g. Arial Unicode MS) predate it and render tofu.
_REGULAR_PATHS = (
    os.path.join(_BUNDLED, "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)
_BOLD_PATHS = (
    os.path.join(_BUNDLED, "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)


@dataclass(frozen=True)
class PdfFonts:
    regular: str
    bold: str
    # False when we fell back to Helvetica: callers avoid non-Latin-1 glyphs.
    unicode: bool


_HELVETICA = PdfFonts(regular="Helvetica", bold="Helvetica-Bold", unicode=False)


def _first_existing(paths: tuple[str, ...]) -> str | None:
    return next((p for p in paths if os.path.exists(p)), None)


def _register() -> PdfFonts:
    regular = _first_existing(_REGULAR_PATHS)
    if regular is None:
        return _HELVETICA
    bold = _first_existing(_BOLD_PATHS) or regular
    try:
        pdfmetrics.registerFont(TTFont(_FAMILY, regular))
        pdfmetrics.registerFont(TTFont(f"{_FAMILY}-Bold", bold))
        # Map the family so inline <b> markup in a Paragraph switches to the bold face.
        for italic in (0, 1):
            addMapping(_FAMILY, 0, italic, _FAMILY)
            addMapping(_FAMILY, 1, italic, f"{_FAMILY}-Bold")
    except Exception:
        return _HELVETICA
    return PdfFonts(regular=_FAMILY, bold=f"{_FAMILY}-Bold", unicode=True)


# Registered once at import; reportlab's font registry is process-global.
PDF_FONTS = _register()
