"""Monthly payroll Excel (.xlsx) builder — pure, deterministic, no I/O beyond an
in-memory buffer.

Given the per-employee rows for a month (identity + bank details + UAN + the
LOP-adjusted salary breakdown), produce a single-sheet workbook and return the
raw bytes. Money is written as whole-currency **numbers** (not strings) so totals
sum in Excel. Decoupled from the ORM, like `payslip_pdf`.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import cast

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

# (header, is_money) in column order. Money columns are prorated (LOP-adjusted).
_COLUMNS: tuple[tuple[str, bool], ...] = (
    ("Employee", False),
    ("Department", False),
    ("UAN", False),
    ("Account holder", False),
    ("Bank", False),
    ("Account number", False),
    ("IFSC", False),
    ("Account type", False),
    ("Present days", False),
    ("Payable days", False),
    ("Total days", False),
    ("Basic", True),
    ("HRA", True),
    ("Special allowance", True),
    ("Gross", True),
    ("Provident fund", True),
    ("Professional tax", True),
    ("Income tax (TDS)", True),
    ("Total deductions", True),
    ("Net pay", True),
)

_HEADER_FILL = PatternFill("solid", fgColor="5A48E0")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


@dataclass(frozen=True)
class PayrollExportRow:
    """One employee's line for the month. Money fields are whole currency units
    (already LOP-adjusted); day fields are counts."""

    employee: str
    department: str
    uan: str
    account_holder: str
    bank_name: str
    account_number: str
    ifsc: str
    account_type: str
    present_days: float
    payable_days: float
    total_days: int
    basic: int
    hra: int
    special_allowance: int
    gross: int
    provident_fund: int
    professional_tax: int
    income_tax: int
    total_deductions: int
    net_pay: int

    def as_cells(self) -> list[object]:
        return [
            self.employee,
            self.department,
            self.uan,
            self.account_holder,
            self.bank_name,
            self.account_number,
            self.ifsc,
            self.account_type,
            self.present_days,
            self.payable_days,
            self.total_days,
            self.basic,
            self.hra,
            self.special_allowance,
            self.gross,
            self.provident_fund,
            self.professional_tax,
            self.income_tax,
            self.total_deductions,
            self.net_pay,
        ]


def _autofit(ws: Worksheet, rows: list[PayrollExportRow]) -> None:
    for idx, (header, _is_money) in enumerate(_COLUMNS, start=1):
        widest = len(header)
        for row in rows:
            widest = max(widest, len(str(row.as_cells()[idx - 1])))
        ws.column_dimensions[get_column_letter(idx)].width = min(widest + 2, 40)


def build_payroll_xlsx(rows: list[PayrollExportRow], *, month_label: str, currency: str) -> bytes:
    """Render the month's payroll rows to .xlsx bytes."""
    wb = Workbook()
    ws = cast(Worksheet, wb.active)  # a fresh Workbook always has an active sheet
    ws.title = "Payroll"

    money_fmt = f'"{currency}" #,##0'
    ws.append([f"Payroll — {month_label}"])
    ws.append([f"Amounts prorated for loss of pay · {currency}"])
    ws.append([])  # spacer row

    header_row_idx = ws.max_row + 1
    ws.append([header for header, _ in _COLUMNS])
    for col in range(1, len(_COLUMNS) + 1):
        cell = ws.cell(row=header_row_idx, column=col)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append(row.as_cells())
        written = ws.max_row
        for col, (_header, is_money) in enumerate(_COLUMNS, start=1):
            if is_money:
                ws.cell(row=written, column=col).number_format = money_fmt

    ws.freeze_panes = f"A{header_row_idx + 1}"
    _autofit(ws, rows)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
