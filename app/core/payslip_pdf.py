"""Payslip PDF rendering — pure, deterministic, no I/O beyond an in-memory buffer.

Given a frozen `PayslipPdfData` snapshot, render a single-page A4 salary slip: an
identity header (name, title, department, location, date of joining), a
three-column ledger — Component / Monthly (full entitlement) / This month
(prorated to days paid) — for earnings then deductions, the net-payable hero, and
the attendance line that explains the proration. Built with reportlab's built-in
Helvetica so there are no font-embedding or system dependencies. The same bytes
are served for download and attached to the email.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_VIOLET = colors.HexColor("#5a48e0")
_INK = colors.HexColor("#1a1530")
_MUTED = colors.HexColor("#6b6485")
_LINE = colors.HexColor("#e6e2f0")
_PAPER = colors.HexColor("#f6f4ff")

# Built-in Helvetica (WinAnsi) has no ₹ glyph — it renders as tofu — so INR uses
# an ASCII "Rs" prefix in the PDF. The web UI renders ₹ fine via the browser.
_CURRENCY_SYMBOLS = {"INR": "Rs ", "USD": "$", "EUR": "€", "GBP": "£"}


@dataclass(frozen=True)
class PayslipPdfData:
    """Everything the PDF needs, decoupled from the ORM. Money in minor units.

    `monthly` is the full-month breakdown and `prorated` the same slip scaled to
    the days actually paid; both are the `SalaryBreakdownRead` field dicts.
    """

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


def _money(minor: int, currency: str) -> str:
    # Round to the nearest whole unit (matches the frontend `formatMoney` and the
    # payslip's rupee figures); prorated paise otherwise truncate ₹1 low.
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{round(minor / 100):,}"


def _days(value: float) -> str:
    return f"{value:g}"


def _style(name: str, *, size: float, color: colors.Color, **kw: object) -> ParagraphStyle:
    kw.setdefault("fontName", "Helvetica")
    return ParagraphStyle(name, fontSize=size, textColor=color, **kw)


def _eyebrow(text: str) -> Paragraph:
    return Paragraph(
        text.upper(),
        _style("eyebrow", size=8, color=_MUTED, leading=11, fontName="Helvetica-Bold"),
    )


def _ledger_rows(data: PayslipPdfData) -> list[list[object]]:
    """Component / Monthly / This-month rows: earnings, gross, deductions, totals."""
    label = _style("cell", size=9.5, color=_INK, leading=14)
    muted = _style("cellm", size=9.5, color=_MUTED, leading=14)
    bold = _style("b", size=9.5, color=_INK, leading=14, fontName="Helvetica-Bold")
    head = _style("h", size=8.5, color=_MUTED, leading=12, fontName="Helvetica-Bold")
    c = data.currency

    def line(label_text: str, key: str, *, strong: bool = False) -> list[object]:
        style = bold if strong else label
        return [
            Paragraph(label_text, style),
            Paragraph(_money(data.monthly.get(key, 0), c), style),
            Paragraph(_money(data.prorated.get(key, 0), c), style),
        ]

    def section(title: str) -> list[object]:
        return [Paragraph(title, head), Paragraph("MONTHLY", head), Paragraph("THIS MONTH", head)]

    return [
        section("Earnings"),
        line("Basic", "basic_minor"),
        line("HRA", "hra_minor"),
        line("Special allowance", "special_allowance_minor"),
        line("Gross earnings", "gross_minor", strong=True),
        [Paragraph("Deductions", head), Paragraph("", muted), Paragraph("", muted)],
        line("Provident fund", "employee_pf_minor"),
        line("Professional tax", "professional_tax_minor"),
        line("Income tax (TDS)", "income_tax_minor"),
        line("Total deductions", "total_deduction_minor", strong=True),
    ]


def _identity_block(data: PayslipPdfData) -> Table:
    value = _style("v", size=10.5, color=_INK, leading=15)
    strong = _style("vs", size=12, color=_INK, leading=16, fontName="Helvetica-Bold")
    role = " · ".join(p for p in (data.job_title, data.department) if p) or "—"
    ident = Table(
        [
            [_eyebrow("Employee"), _eyebrow("Location"), _eyebrow("Pay period")],
            [
                Paragraph(data.employee_name, strong),
                Paragraph(data.location or "—", value),
                Paragraph(data.month_label, value),
            ],
            [_eyebrow("Title · Department"), _eyebrow("Date of joining"), _eyebrow("")],
            [
                Paragraph(role, value),
                Paragraph(data.doj_label or "—", value),
                Paragraph("", value),
            ],
        ],
        colWidths=[74 * mm, 50 * mm, 50 * mm],
    )
    ident.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PAPER),
                ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, 0), 9),
                ("TOPPADDING", (0, 2), (-1, 2), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
            ]
        )
    )
    return ident


def _net_block(data: PayslipPdfData) -> Table:
    net_block = Table(
        [
            [
                Paragraph(
                    "NET PAYABLE",
                    _style("nl", size=9, color=colors.white, leading=12, fontName="Helvetica-Bold"),
                ),
                Paragraph(
                    _money(data.net_payable_minor, data.currency),
                    _style(
                        "nv",
                        size=20,
                        color=colors.white,
                        leading=24,
                        fontName="Helvetica-Bold",
                        alignment=2,
                    ),
                ),
            ]
        ],
        colWidths=[90 * mm, 84 * mm],
    )
    net_block.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _VIOLET),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 13),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
            ]
        )
    )
    return net_block


def render_payslip_pdf(data: PayslipPdfData) -> bytes:
    """Render one month's released payslip to PDF bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=f"Payslip — {data.employee_name} — {data.month_label}",
        author="Avora",
    )
    flow: list[object] = []

    # --- masthead ---
    flow.append(
        Paragraph(
            data.org_name,
            _style("org", size=18, color=_INK, leading=22, fontName="Helvetica-Bold"),
        )
    )
    flow.append(
        Paragraph(
            f"Payslip for {data.month_label}",
            _style("sub", size=10.5, color=_MUTED, leading=16),
        )
    )
    flow.append(Spacer(1, 10))

    # --- employee identity block ---
    flow.append(_identity_block(data))
    flow.append(Spacer(1, 14))

    # --- Component / Monthly / This-month ledger ---
    ledger = Table(_ledger_rows(data), colWidths=[74 * mm, 50 * mm, 50 * mm])
    ledger.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _LINE),  # under Earnings header
                ("LINEBELOW", (0, 4), (-1, 4), 1, _INK),  # under Gross earnings
                ("LINEBELOW", (0, 5), (-1, 5), 0.5, _LINE),  # under Deductions header
                ("LINEABOVE", (0, -1), (-1, -1), 1, _INK),  # over Total deductions
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (1, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    flow.append(ledger)
    flow.append(Spacer(1, 16))

    # --- net payable hero + attendance proration ---
    lop = max(0.0, data.working_days - data.present_days - data.paid_leave_days)
    note = (
        f"Full-month net is {_money(data.monthly.get('net_minor', 0), data.currency)}; "
        f"paid for {_days(data.payable_days)} of {data.total_days} calendar days. "
        f"Of {data.working_days} working days: {_days(data.present_days)} present, "
        f"{_days(data.paid_leave_days)} paid leave, {_days(lop)} loss of pay "
        f"(weekends and holidays are paid)."
    )
    flow.append(_net_block(data))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(note, _style("note", size=8.5, color=_MUTED, leading=13)))
    flow.append(Spacer(1, 22))

    # --- footer ---
    flow.append(
        Paragraph(
            f"Generated by Avora on {data.generated_label}. This is a system-generated "
            "payslip and does not require a signature.",
            _style("foot", size=8, color=_MUTED, leading=12),
        )
    )

    doc.build(flow)
    return buf.getvalue()
