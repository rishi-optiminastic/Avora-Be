"""Render Access spend to an .xlsx workbook for finance.

Three sheets, built from the same `SpendLine`s the screen shows:

* Summary - monthly and annual totals per currency, then per team and per
  category.
* Line items - every costed asset with its cycle, monthly and annual figures,
  owner, team, billed-to account and payment details.
* Renewals - what renews in the next 90 days.

Money is written as currency units (not minor units) in number cells, so the
sheet sums and filters like any finance sheet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from io import BytesIO
from typing import cast

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.access_spend import SpendLine, SpendTotals, group_totals, totals

_HEADER_FILL = PatternFill("solid", fgColor="EBE7F2")
_HEADER_FONT = Font(bold=True, color="1A1530")
_TITLE_FONT = Font(bold=True, size=13, color="1A1530")
_MONEY = "#,##0.00"
_MINOR = 100

Cell = str | int | float | date | None


def _units(minor: int) -> float:
    return minor / _MINOR


def _header(ws: Worksheet, row: int, headers: Sequence[str]) -> None:
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT


def _widths(ws: Worksheet, widths: Sequence[int]) -> None:
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width


def _currencies(lines: Sequence[SpendLine]) -> list[str]:
    found = sorted({line.item.currency for line in lines if line.counted})
    return sorted(found, key=lambda c: (c != "INR", c))


def _total_rows(
    ws: Worksheet, start: int, label: str, groups: list[tuple[str, SpendTotals]], cur: list[str]
) -> int:
    """Write one titled block of totals and return the next free row."""
    ws.cell(row=start, column=1, value=label).font = _TITLE_FONT
    headers = [label.split(" ")[-1].capitalize(), "Items"]
    for c in cur:
        headers += [f"Monthly ({c})", f"Annual ({c})"]
    _header(ws, start + 1, headers)
    row = start + 2
    for name, t in groups:
        values: list[Cell] = [name, t.count]
        for c in cur:
            values += [_units(t.monthly.get(c, 0)), _units(t.annual.get(c, 0))]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            if col > 2:
                cell.number_format = _MONEY
        row += 1
    return row + 1


def _summary_sheet(ws: Worksheet, lines: Sequence[SpendLine], generated: date) -> None:
    ws.title = "Summary"
    cur = _currencies(lines)
    ws.cell(row=1, column=1, value="Tech spend").font = _TITLE_FONT
    ws.cell(row=2, column=1, value=f"Generated {generated.isoformat()} from Avora Access")
    overall = totals(lines)
    row = _total_rows(ws, 4, "Total spend", [("All live subscriptions", overall)], cur)
    row = _total_rows(ws, row, "Spend by team", group_totals(lines, "team"), cur)
    _total_rows(ws, row, "Spend by category", group_totals(lines, "category"), cur)
    _widths(ws, [32, 8, *([18] * (2 * max(len(cur), 1)))])


_LINE_HEADERS = (
    "Name",
    "Kind",
    "Category",
    "Team",
    "Owner",
    "Billed to account",
    "Status",
    "Billing cycle",
    "Currency",
    "Cost per bill",
    "Monthly",
    "Annual",
    "Next renewal",
    "Payment method",
    "Card last 4",
    "Auto-renew",
    "Counted in totals",
    "Estimate",
)


def _line_values(line: SpendLine) -> list[Cell]:
    item = line.item
    return [
        item.name,
        item.kind,
        line.category,
        line.team,
        item.owner_name or "",
        item.billed_to or "",
        item.status,
        item.billing_cycle or "",
        item.currency,
        _units(item.cost_minor),
        _units(line.monthly_minor),
        _units(line.annual_minor),
        item.renews_on,
        item.payment_method or "",
        item.card_last4 or "",
        item.auto_renew or "",
        "Yes" if line.counted else "No",
        "Yes" if line.estimated else "No",
    ]


def _lines_sheet(ws: Worksheet, title: str, lines: Sequence[SpendLine]) -> None:
    ws.title = title
    _header(ws, 1, _LINE_HEADERS)
    for row, line in enumerate(lines, start=2):
        for col, value in enumerate(_line_values(line), start=1):
            cell = ws.cell(row=row, column=col, value=value)
            if col in (10, 11, 12):
                cell.number_format = _MONEY
            elif col == 13 and value is not None:
                cell.number_format = "dd-mmm-yyyy"
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(_LINE_HEADERS))}{max(len(lines) + 1, 1)}"
    _widths(ws, [30, 12, 16, 18, 18, 30, 10, 14, 9, 13, 13, 13, 14, 16, 11, 10, 10, 9])


def build_spend_xlsx(
    lines: Sequence[SpendLine], renewals: Sequence[SpendLine], *, generated: date
) -> bytes:
    wb = Workbook()
    _summary_sheet(cast(Worksheet, wb.active), lines, generated)
    _lines_sheet(wb.create_sheet(), "Line items", lines)
    _lines_sheet(wb.create_sheet(), "Renewals (90 days)", renewals)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
