"""Probation confirmation and extension letters - HR's wording, rendered to PDF.

Pure and deterministic: a frozen snapshot in, PDF bytes out, no I/O beyond an
in-memory buffer. The text follows HR's two templates line for line; only the
bracketed placeholders are filled.

Every value that came from a person (name, job title, expectations) is passed
through `_text` before it reaches a reportlab `Paragraph`, which parses a small
XML-like markup. Unescaped, a job title of `<font size=90>` restyles the letter
and a stray `<` makes the build raise.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from app.core.pdf_fonts import PDF_FONTS

# HR's confirmation template names "Optiminastic Infomedia Pvt. Ltd." in its
# body while every other line, and the extension letter, says Media. One name,
# used everywhere, so a letter never contradicts its own sign-off.
COMPANY_LEGAL_NAME = "Optiminastic Media Pvt. Ltd."
SIGNATORY_NAME = "Akshae Golekar"
SIGNATORY_TITLE = "CEO"

# Bounds HR's extension template allows for: a short, numbered list.
MAX_EXPECTATIONS = 5
MAX_EXPECTATION_CHARS = 300

_INK = colors.HexColor("#1a1530")
_LINE = colors.HexColor("#e6e2f0")

_MARGIN = 24 * mm
_SIGNATURE_GAP = 16 * mm
# C0 controls (bar tab/newline, which we fold to spaces) and DEL. reportlab can
# choke on them and they never belong in a letter.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_BULLET = chr(0x2022)  # spelled as a code point to keep the source ASCII


@dataclass(frozen=True)
class ConfirmationLetter:
    issued_on: date
    employee_name: str
    job_title: str
    effective_date: date


@dataclass(frozen=True)
class ExtensionLetter:
    issued_on: date
    employee_name: str
    job_title: str
    original_end_date: date
    new_end_date: date
    expectations: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _text(value: str) -> str:
    """User text made safe for Paragraph markup: one line, no controls, escaped."""
    cleaned = _CONTROL_CHARS.sub("", value)
    return escape(_WHITESPACE.sub(" ", cleaned).strip())


def _long_date(value: date) -> str:
    """16 October 2026 - no zero padding, the way a letter writes it."""
    return f"{value.day} {value:%B %Y}"


def _day_and_date(value: date) -> str:
    """Tuesday, 27 October 2026 - the template asks for the day and the date."""
    return f"{value:%A}, {_long_date(value)}"


def _style(name: str, *, bold: bool = False, **kw: object) -> ParagraphStyle:
    kw.setdefault("fontSize", 10)
    kw.setdefault("leading", 14.5)
    kw.setdefault("textColor", _INK)
    kw.setdefault("spaceAfter", 6)
    return ParagraphStyle(name, fontName=PDF_FONTS.bold if bold else PDF_FONTS.regular, **kw)


_BODY = _style("body")
_TIGHT = _style("tight", spaceAfter=0)
_SUBJECT = _style("subject", bold=True, spaceBefore=12, spaceAfter=10)
_HEADING = _style("heading", bold=True, spaceBefore=8, spaceAfter=5)
_COMPANY = _style("company", bold=True, fontSize=15, leading=19, spaceAfter=0)
_ITEM = _style("item", leftIndent=8 * mm, bulletIndent=2 * mm, spaceAfter=4)


def _para(markup: str) -> Paragraph:
    return Paragraph(markup, _BODY)


def _numbered(items: Sequence[str]) -> list[Flowable]:
    """`items` is already-safe markup."""
    return [Paragraph(item, _ITEM, bulletText=f"{n}.") for n, item in enumerate(items, start=1)]


def _bulleted(items: Sequence[str]) -> list[Flowable]:
    return [Paragraph(item, _ITEM, bulletText=_BULLET) for item in items]


# --------------------------------------------------------------------------- #
# Shared blocks
# --------------------------------------------------------------------------- #
def _masthead() -> list[Flowable]:
    return [
        Paragraph(escape(COMPANY_LEGAL_NAME), _COMPANY),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.75, color=_LINE, spaceBefore=0, spaceAfter=14),
    ]


def _addressee(issued_on: date, name: str, title: str, subject: str) -> list[Flowable]:
    return [
        Paragraph(f"Date: {_long_date(issued_on)}", _BODY),
        Spacer(1, 4),
        Paragraph(f"<b>{name}</b>", _TIGHT),
        Paragraph(title, _TIGHT),
        Paragraph(f"Subject: {escape(subject)}", _SUBJECT),
        _para(f"Dear {name},"),
    ]


def _sign_off(last_line: Flowable, closing: str) -> list[Flowable]:
    """The letter's last line, closing and signature kept on one page: a
    signature stranded alone on a second page reads as detached from the letter."""
    block: list[Flowable] = [
        last_line,
        Paragraph(escape(closing), _BODY),
        Paragraph("Authorized Signatory", _TIGHT),
        Paragraph(f"For {escape(COMPANY_LEGAL_NAME)}", _TIGHT),
        Spacer(1, _SIGNATURE_GAP),
        Paragraph(f"<b>{escape(SIGNATORY_NAME)}</b>", _TIGHT),
        Paragraph(escape(SIGNATORY_TITLE), _TIGHT),
    ]
    return [Spacer(1, 6), KeepTogether(block)]


def _render(flow: list[Flowable], title: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author=COMPANY_LEGAL_NAME,
        creator="Avora",
    )
    doc.build(flow)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# The two letters
# --------------------------------------------------------------------------- #
def confirmation_story(letter: ConfirmationLetter) -> list[Flowable]:
    name, title = _text(letter.employee_name), _text(letter.job_title)
    company = escape(COMPANY_LEGAL_NAME)
    return [
        *_masthead(),
        *_addressee(letter.issued_on, name, title, "Confirmation of Employment"),
        _para(
            "We are pleased to inform you that following the successful completion of your "
            f"probationary period, your employment with {company} as <b>{title}</b> has been "
            f"confirmed, and will be effective from <b>{_day_and_date(letter.effective_date)}</b>."
        ),
        _para(
            "During your probation period, you demonstrated exceptional skills, a positive "
            "attitude, and alignment with our company's ethics and values."
        ),
        _para(
            "We recognize your excellent performance to our company and look forward to your "
            "continued growth and success within our organization."
        ),
        Paragraph("Next Steps", _HEADING),
        *_bulleted(
            [
                'Your employment status has been updated to "Permanent/Confirmed" and will be '
                "reflected accordingly on Avora.",
                "You will be covered under the company's insurance policy in accordance with the "
                "terms outlined in your appointment letter.",
            ]
        ),
        Spacer(1, 4),
        _para(
            "We thank you for your hard work and continued dedication. We are excited to have "
            "you on board as a confirmed member of our team and look forward to our continued "
            "success."
        ),
        _para("Should you have any questions, please feel free to reach out to the HR Department."),
        *_sign_off(_para("Congratulations on this achievement!"), "Best regards,"),
    ]


def extension_story(letter: ExtensionLetter) -> list[Flowable]:
    name, title = _text(letter.employee_name), _text(letter.job_title)
    new_end = _long_date(letter.new_end_date)
    return [
        *_masthead(),
        *_addressee(letter.issued_on, name, title, "Probation Period Extension"),
        _para(
            "Following the review of your performance during your probationary period, we "
            "would like to inform you that your probation period with "
            f"{escape(COMPANY_LEGAL_NAME)} as <b>{title}</b> has been extended."
        ),
        _para(
            "This decision is based on Performance Criteria of Probation Evaluation. Your "
            "probation period, which was originally set to end on "
            f"<b>{_long_date(letter.original_end_date)}</b>, has now been extended to "
            f"<b>{new_end}</b>."
        ),
        Paragraph("Expectations During Extended Probation", _HEADING),
        _para("To successfully complete this extended probation period, you are expected to:"),
        *_numbered([_text(item) for item in letter.expectations]),
        Paragraph("Terms of the Extension", _HEADING),
        *_numbered(
            [
                f"New Probation End Date: <b>{new_end}</b>.",
                "Performance Reviews: Your performance will be closely monitored and reviewed "
                "periodically.",
                "Outcome: Upon completion of your extended probationary period, a final decision "
                "regarding your employment status will be made.",
            ]
        ),
        Spacer(1, 4),
        _para(
            "We encourage you to take this opportunity to demonstrate your capabilities and "
            "focus on the specific areas mentioned above for improvement."
        ),
        _para(
            "Please feel free to reach out to your supervisor or HR representative if you have "
            "any questions and concerns or need any guidance."
        ),
        *_sign_off(
            _para("We are confident in your potential and look forward to seeing your progress."),
            "Regards,",
        ),
    ]


def render_confirmation_letter(letter: ConfirmationLetter) -> bytes:
    return _render(confirmation_story(letter), "Confirmation of Employment")


def render_extension_letter(letter: ExtensionLetter) -> bytes:
    return _render(extension_story(letter), "Probation Period Extension")
