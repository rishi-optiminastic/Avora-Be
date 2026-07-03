"""Payslip PDF rendering — pure, deterministic, no I/O beyond an in-memory buffer.

Given a frozen `PayslipPdfData` snapshot, render a single-page A4 salary slip
(Zoho-Payroll-style: identity block, a two-column earnings/deductions table, the
net-payable hero, and an attendance line) and return the raw PDF bytes. Built
with reportlab's built-in Helvetica so there are no font-embedding or system
dependencies. The same bytes are served for download and attached to the email.
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

_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


@dataclass(frozen=True)
class PayslipPdfData:
    """Everything the PDF needs, decoupled from the ORM. Money in minor units."""

    org_name: str
    employee_name: str
    department: str | None
    month_label: str
    currency: str
    monthly_ctc_minor: int
    basic_minor: int
    hra_minor: int
    special_allowance_minor: int
    gross_minor: int
    employee_pf_minor: int
    professional_tax_minor: int
    income_tax_minor: int
    total_deduction_minor: int
    net_minor: int  # full-month net (gross - deductions)
    net_payable_minor: int  # prorated take-home actually paid this month
    working_days: int
    present_days: float
    paid_leave_days: float
    payable_days: float
    generated_label: str


def _money(minor: int, currency: str) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{minor // 100:,}"


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


def _amount_rows(data: PayslipPdfData) -> list[list[object]]:
    """The earnings (left) / deductions (right) ledger, row-aligned."""
    earnings = [
        ("Basic", data.basic_minor),
        ("HRA", data.hra_minor),
        ("Special allowance", data.special_allowance_minor),
    ]
    deductions = [
        ("Provident fund", data.employee_pf_minor),
        ("Professional tax", data.professional_tax_minor),
        ("Income tax (TDS)", data.income_tax_minor),
    ]
    label = _style("cell", size=9.5, color=_INK, leading=14)
    muted = _style("cellm", size=9.5, color=_MUTED, leading=14)
    rows: list[list[object]] = []
    for (e_label, e_val), (d_label, d_val) in zip(earnings, deductions, strict=True):
        rows.append(
            [
                Paragraph(e_label, label),
                Paragraph(_money(e_val, data.currency), label),
                Paragraph(d_label, muted if not d_label else label),
                Paragraph("" if d_val < 0 else _money(d_val, data.currency), label),
            ]
        )
    return rows


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

    # --- masthead: org + payslip period ---
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
    ident = Table(
        [
            [_eyebrow("Employee"), _eyebrow("Department"), _eyebrow("Pay period")],
            [
                Paragraph(
                    data.employee_name,
                    _style("v", size=11, color=_INK, leading=15, fontName="Helvetica-Bold"),
                ),
                Paragraph(data.department or "—", _style("v2", size=11, color=_INK, leading=15)),
                Paragraph(data.month_label, _style("v3", size=11, color=_INK, leading=15)),
            ],
        ],
        colWidths=[68 * mm, 53 * mm, 53 * mm],
    )
    ident.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PAPER),
                ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
            ]
        )
    )
    flow.append(ident)
    flow.append(Spacer(1, 14))

    # --- earnings / deductions ledger ---
    head = _style("h", size=8.5, color=_MUTED, leading=12, fontName="Helvetica-Bold")
    header = [
        Paragraph("EARNINGS", head),
        Paragraph("", head),
        Paragraph("DEDUCTIONS", head),
        Paragraph("", head),
    ]
    bold = _style("b", size=9.5, color=_INK, leading=14, fontName="Helvetica-Bold")
    totals = [
        Paragraph("Gross earnings", bold),
        Paragraph(_money(data.gross_minor, data.currency), bold),
        Paragraph("Total deductions", bold),
        Paragraph(_money(data.total_deduction_minor, data.currency), bold),
    ]
    ledger = Table(
        [header, *_amount_rows(data), totals],
        colWidths=[44 * mm, 43 * mm, 44 * mm, 43 * mm],
    )
    ledger.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _LINE),
                ("LINEABOVE", (0, -1), (-1, -1), 1, _INK),
                ("LINEAFTER", (1, 0), (1, -1), 0.5, _LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (1, 0), (1, -1), 14),
                ("LEFTPADDING", (2, 0), (2, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            ]
        )
    )
    flow.append(ledger)
    flow.append(Spacer(1, 16))

    # --- net payable hero + attendance proration ---
    note = (
        f"Net for the full month is {_money(data.net_minor, data.currency)}; "
        f"prorated to {_days(data.payable_days)} of {data.working_days} working days "
        f"(present {_days(data.present_days)} + paid leave {_days(data.paid_leave_days)})."
    )
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
    flow.append(net_block)
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
