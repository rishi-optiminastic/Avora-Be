"""Payslip PDF rendering — pure, deterministic, no I/O beyond an in-memory buffer.

Given a frozen `PayslipPdfData` snapshot, render a single-page A4 salary slip in the
familiar Indian-payroll layout: a company masthead (name + address + optional
logo), a Pay Summary (identity on the left, a Total-Net-Pay hero on the right),
side-by-side Earnings and Deductions ledgers with an Amount + YTD column, and a
"Total Net Payable" banner with the amount spelled out in words.

The rupee sign needs a Unicode font — built-in Helvetica has no ₹ glyph. We
register the first DejaVu/Noto/Arial-Unicode TTF we can find at import time and
fall back to Helvetica + an ASCII "Rs" prefix when none is present, so the PDF
always renders (just without the ₹ symbol) on a bare container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_INK = colors.HexColor("#1a1530")
_MUTED = colors.HexColor("#8a8598")
_LINE = colors.HexColor("#e6e2f0")
_HEAD_BG = colors.HexColor("#f4f4f7")
_GREEN = colors.HexColor("#1f9d55")
_GREEN_SOFT = colors.HexColor("#eaf7ef")


def _register_unicode_font() -> tuple[str, str, str]:
    """Register a ₹-capable TTF if one is available. Returns (regular, bold, ₹).

    Falls back to Helvetica + "Rs " when no Unicode font is found so the PDF still
    renders on a minimal container (add `fonts-dejavu-core` to the image for ₹)."""
    bundled = os.path.join(os.path.dirname(__file__), "fonts")
    # The bundled DejaVu is tried first — it definitely has ₹ (U+20B9); many system
    # "Unicode" fonts (e.g. Arial Unicode MS) predate the sign and render tofu.
    regular_paths = [
        os.path.join(bundled, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    bold_paths = [
        os.path.join(bundled, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    regular = next((p for p in regular_paths if os.path.exists(p)), None)
    if regular is None:
        return "Helvetica", "Helvetica-Bold", "Rs "
    bold = next((p for p in bold_paths if os.path.exists(p)), regular)
    try:
        pdfmetrics.registerFont(TTFont("PayslipSans", regular))
        pdfmetrics.registerFont(TTFont("PayslipSans-Bold", bold))
    except Exception:
        return "Helvetica", "Helvetica-Bold", "Rs "
    return "PayslipSans", "PayslipSans-Bold", "₹"


_FONT, _FONT_BOLD, _RUPEE = _register_unicode_font()
_SYMBOLS = {"INR": _RUPEE, "USD": "$", "EUR": "€", "GBP": "£"}


@dataclass(frozen=True)
class PayslipPdfData:
    """Everything the PDF needs, decoupled from the ORM. Money in minor units.

    `monthly` is the full-month breakdown and `prorated` the same slip scaled to
    the days actually paid; both are `SalaryBreakdownRead` field dicts. `ytd` maps
    the same component keys to the year-to-date total (optional)."""

    org_name: str
    employee_name: str
    job_title: str | None
    department: str | None
    location: str | None
    doj_label: str | None  # date of joining, pre-formatted (e.g. "12 Jan 2024")
    month_label: str
    currency: str
    monthly_ctc_minor: int
    monthly: dict[str, int]
    prorated: dict[str, int]
    net_payable_minor: int  # prorated take-home actually paid this month
    total_days: int  # calendar days in the month (the proration base)
    working_days: int  # weekdays minus holidays
    present_days: float
    paid_leave_days: float
    payable_days: float
    generated_label: str
    # Optional extras for the register-style slip (default-safe).
    # Approved expense claims paid out with this month's salary. Shown as its own
    # earnings row and folded into Gross Earnings so the slip's stated formula
    # (Net = Gross - Deductions) still adds up. Not part of `prorated`, because a
    # reimbursement repays money already spent and must not be taxed or PF'd.
    reimbursement_minor: int = 0
    org_address: str | None = None
    employee_number: str | None = None
    pay_date_label: str | None = None
    bank_account: str | None = None
    logo_png: bytes | None = None
    ytd: dict[str, int] = field(default_factory=dict)


def _money(minor: int, currency: str) -> str:
    symbol = _SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{round(minor / 100):,}"


def _days(value: float) -> str:
    return f"{value:g}"


def _style(
    name: str, *, size: float, color: colors.Color, bold: bool = False, **kw: object
) -> ParagraphStyle:
    kw.setdefault("fontName", _FONT_BOLD if bold else _FONT)
    return ParagraphStyle(name, fontSize=size, textColor=color, **kw)


# --------------------------------------------------------------------------- #
# Amount in words (Indian numbering: thousand / lakh / crore)
# --------------------------------------------------------------------------- #
_ONES = [
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ("-" + _ONES[ones] if ones else "")


def _three_digits(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_two_digits(rest))
    return " ".join(parts)


def amount_in_words(rupees: int, currency: str) -> str:
    """ "Indian Rupee Thirty-Five Thousand Sixty-Four Only" — Indian grouping."""
    prefix = {"INR": "Indian Rupee", "USD": "US Dollar", "EUR": "Euro", "GBP": "Pound"}.get(
        currency.upper(), currency.upper()
    )
    if rupees == 0:
        return f"{prefix} Zero Only"
    crore, rest = divmod(rupees, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, hundreds = divmod(rest, 1_000)
    chunks = []
    if crore:
        chunks.append(f"{_three_digits(crore)} Crore")
    if lakh:
        chunks.append(f"{_two_digits(lakh)} Lakh")
    if thousand:
        chunks.append(f"{_two_digits(thousand)} Thousand")
    if hundreds:
        chunks.append(_three_digits(hundreds))
    return f"{prefix} {' '.join(chunks)} Only"


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #
def _masthead(data: PayslipPdfData) -> Table:
    org = _style("org", size=17, color=_INK, bold=True, leading=21)
    addr = _style("addr", size=8.5, color=_MUTED, leading=12)
    left = [Paragraph(data.org_name, org)]
    if data.org_address:
        left.append(Paragraph(data.org_address, addr))
    logo_cell: object = ""
    if data.logo_png:
        try:
            img = Image(BytesIO(data.logo_png))
            ratio = img.imageHeight / img.imageWidth if img.imageWidth else 0.25
            img.drawWidth = 42 * mm
            img.drawHeight = min(18 * mm, 42 * mm * ratio)
            img.hAlign = "RIGHT"
            logo_cell = img
        except Exception:
            logo_cell = ""
    t = Table([[left, logo_cell]], colWidths=[124 * mm, 50 * mm])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return t


def _summary_block(data: PayslipPdfData) -> Table:
    label = _style("l", size=9, color=_MUTED, leading=17)
    value = _style("v", size=9.5, color=_INK, leading=17)
    name = data.employee_name + (f", {data.employee_number}" if data.employee_number else "")
    rows = [
        ("Employee Name", name),
        ("Designation", data.job_title or "—"),
        ("Date of Joining", data.doj_label or "—"),
        ("Pay Period", data.month_label.replace("Payslip for ", "")),
        ("Pay Date", data.pay_date_label or "—"),
        ("Bank Account No", data.bank_account or "—"),
    ]
    ident = Table(
        [[Paragraph(k, label), Paragraph(v, value)] for k, v in rows],
        colWidths=[34 * mm, 66 * mm],
    )
    ident.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    lop = max(0.0, data.total_days - data.payable_days)
    net_box = Table(
        [
            [
                Paragraph(
                    "Total Net Pay", _style("nl", size=9, color=_MUTED, leading=13, alignment=1)
                )
            ],
            [
                Paragraph(
                    _money(data.net_payable_minor, data.currency),
                    _style("nv", size=21, color=_GREEN, bold=True, leading=26, alignment=1),
                )
            ],
            [
                Paragraph(
                    f"Paid Days : {_days(data.payable_days)} | LOP Days : {_days(lop)}",
                    _style("nd", size=8.5, color=_MUTED, leading=12, alignment=1),
                )
            ],
        ],
        colWidths=[64 * mm],
    )
    net_box.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.75, _LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (0, 0), 12),
                ("TOPPADDING", (0, 1), (0, 1), 2),
                ("TOPPADDING", (0, 2), (0, 2), 4),
                ("BOTTOMPADDING", (0, 2), (0, 2), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    outer = Table([[ident, net_box]], colWidths=[104 * mm, 70 * mm])
    outer.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return outer


def _ledger(
    title: str,
    rows: list[tuple[str, int, int | None]],
    total_label: str,
    total_minor: int,
    data: PayslipPdfData,
) -> Table:
    """One side of the earnings/deductions grid: header, line items, and a total."""
    c = data.currency
    head = _style("h", size=9, color=_INK, bold=True, leading=13)
    cell = _style("c", size=9, color=_INK, leading=13)
    cellm = _style("cm", size=9, color=_MUTED, leading=13)
    total = _style("t", size=9, color=_INK, bold=True, leading=13)

    body: list[list[object]] = [
        [Paragraph(title, head), Paragraph("Amount", head), Paragraph("YTD", head)]
    ]
    for name, amount, ytd in rows:
        body.append(
            [
                Paragraph(name, cell),
                Paragraph(_money(amount, c), cell),
                Paragraph(_money(ytd, c) if ytd is not None else "", cellm),
            ]
        )
    body.append(
        [
            Paragraph(total_label, total),
            Paragraph(_money(total_minor, c), total),
            Paragraph("", cell),
        ]
    )

    t = Table(body, colWidths=[38 * mm, 25 * mm, 20 * mm])
    last = len(body) - 1
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, -1), 8),
                ("RIGHTPADDING", (-1, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEABOVE", (0, last), (-1, last), 0.75, _LINE),
            ]
        )
    )
    return t


def _earnings_rows(data: PayslipPdfData) -> list[tuple[str, int, int | None]]:
    pro, ytd = data.prorated, data.ytd
    fixed = pro.get("hra_minor", 0) + pro.get("special_allowance_minor", 0)
    fixed_ytd = ytd.get("hra_minor", 0) + ytd.get("special_allowance_minor", 0)
    rows: list[tuple[str, int, int | None]] = [
        ("Basic", pro.get("basic_minor", 0), ytd.get("basic_minor") if ytd else None),
        ("Fixed Allowance", fixed, fixed_ytd if ytd else None),
    ]
    if data.reimbursement_minor > 0:
        rows.append(("Reimbursements", data.reimbursement_minor, None))
    return rows


def _deduction_rows(data: PayslipPdfData) -> list[tuple[str, int, int | None]]:
    pro, ytd = data.prorated, data.ytd
    rows: list[tuple[str, int, int | None]] = [
        (
            "EPF Contribution",
            pro.get("employee_pf_minor", 0),
            ytd.get("employee_pf_minor") if ytd else None,
        ),
        (
            "Professional Tax",
            pro.get("professional_tax_minor", 0),
            ytd.get("professional_tax_minor") if ytd else None,
        ),
    ]
    if pro.get("income_tax_minor", 0) > 0:
        rows.append(
            (
                "Income Tax (TDS)",
                pro.get("income_tax_minor", 0),
                ytd.get("income_tax_minor") if ytd else None,
            )
        )
    return rows


def _net_banner(data: PayslipPdfData) -> Table:
    net_rupees = round(data.net_payable_minor / 100)
    amount = _money(data.net_payable_minor, data.currency)
    words = amount_in_words(net_rupees, data.currency)
    para = Paragraph(
        f'<font name="{_FONT_BOLD}" size="12">Total Net Payable {amount}</font> '
        f'<font size="9" color="#6b6485">({words})</font>',
        _style("nb", size=12, color=_INK, leading=17),
    )
    t = Table([[para]], colWidths=[174 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _GREEN_SOFT),
                ("LINEBEFORE", (0, 0), (0, 0), 3, _GREEN),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 11),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
            ]
        )
    )
    return t


def render_payslip_pdf(data: PayslipPdfData) -> bytes:
    """Render one month's released payslip to PDF bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Payslip — {data.employee_name} — {data.month_label}",
        author="Avora",
    )
    flow: list[object] = [
        _masthead(data),
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.75, color=_LINE, spaceBefore=0, spaceAfter=12),
        Paragraph(
            f"Payslip for the month of {data.month_label}",
            _style("title", size=13, color=_INK, bold=True, leading=18),
        ),
        Spacer(1, 10),
        Paragraph("Pay Summary", _style("ps", size=9.5, color=_INK, bold=True, leading=13)),
        Spacer(1, 8),
        _summary_block(data),
        Spacer(1, 12),
        HRFlowable(width="100%", thickness=0.75, color=_LINE, spaceBefore=0, spaceAfter=14),
    ]

    earnings = _ledger(
        "Earnings",
        _earnings_rows(data),
        "Gross Earnings",
        data.prorated.get("gross_minor", 0) + data.reimbursement_minor,
        data,
    )
    deductions = _ledger(
        "Deductions",
        _deduction_rows(data),
        "Total Deductions",
        data.prorated.get("total_deduction_minor", 0),
        data,
    )
    grid = Table([[earnings, deductions]], colWidths=[87 * mm, 87 * mm])
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 5),
                ("LEFTPADDING", (1, 0), (1, 0), 5),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ]
        )
    )
    flow.append(grid)
    flow.append(Spacer(1, 18))
    flow.append(_net_banner(data))
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            "**Total Net Payable = Gross Earnings - Total Deductions",
            _style("fn", size=8, color=_MUTED, leading=12),
        )
    )
    flow.append(Spacer(1, 26))
    flow.append(
        Paragraph(
            "-- This is a system-generated document. --",
            _style("foot", size=8.5, color=_MUTED, leading=12, alignment=1),
        )
    )

    doc.build(flow)
    return buf.getvalue()
