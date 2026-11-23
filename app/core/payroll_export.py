"""Monthly payroll Excel (.xlsx) builder — the "Payrun Employee Salary statement".

Pure and deterministic (no I/O beyond an in-memory buffer). Given the per-employee
rows for a month, produce a single flat sheet in the 41-column Indian payroll
register layout (one row per employee) and return the raw bytes.

Money is written as whole-currency **numbers** so totals sum in Excel; day counts
(Base Days / Loss Of Pay / Effective Paid Days) use a fixed 30-day base per the
register convention. Decoupled from the ORM, like `payslip_pdf`.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from io import BytesIO
from typing import cast

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.payroll import (
    EDLI_RATE_PCT,
    EPF_ADMIN_RATE_PCT,
    EPS_CAP_MINOR,
    EPS_RATE_PCT,
    PF_WAGE_CEILING_MINOR,
    CalcConfig,
)

# Column kinds drive the cell number format: text / money (whole ₹) / days (2dp).
_TEXT, _MONEY, _DAYS = "text", "money", "days"

# (header, field-name, kind, formula) in exact register order — matches the
# reference sheet. The formula is the one the "Formulas" sheet prints for that
# column: kept HERE, beside the field it explains, because a derivation written
# anywhere else drifts the first time a column moves. `{...}` placeholders are
# filled from the month's actual settings, so the sheet quotes the rates that
# were really used rather than the ones someone typed into a document once.
_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("Period", "period", _TEXT, "The payroll month this register covers."),
    (
        "Payroll Type",
        "payroll_type",
        _TEXT,
        "Regular Payroll, or Reimbursement on an expenses-only run.",
    ),
    ("Employee No", "employee_no", _TEXT, "Employee record."),
    ("Employee Name", "employee_name", _TEXT, "Employee record."),
    ("Department", "department", _TEXT, "Employee record."),
    ("Designation", "designation", _TEXT, "Job title on the employee record."),
    ("Work Location", "work_location", _TEXT, "Employee record."),
    ("Date of Joining", "date_of_joining", _TEXT, "Hire date, dd-mm-yyyy."),
    ("Date of Birth", "date_of_birth", _TEXT, "Employee record, dd-mm-yyyy."),
    ("Last Working Day", "last_working_day", _TEXT, "Blank on a regular run."),
    ("Payment Mode", "payment_mode", _TEXT, "Compensation record; Bank Transfer when unset."),
    ("Account Holder Name", "account_holder", _TEXT, "Compensation record."),
    ("Bank Name", "bank_name", _TEXT, "Compensation record."),
    (
        "Account Number",
        "account_number",
        _TEXT,
        "Compensation record, decrypted for this file only.",
    ),
    ("IFSC", "ifsc", _TEXT, "Compensation record."),
    (
        "CTC Amount(Per Annum)",
        "ctc_annual",
        _MONEY,
        "Monthly CTC x 12. Contractual, never prorated.",
    ),
    (
        "Gross Amount(Per Annum)",
        "gross_annual",
        _MONEY,
        "Full-month Gross x 12. Contractual, never prorated.",
    ),
    (
        "Base Days",
        "base_days",
        _DAYS,
        "Calendar days on the payroll this month: all {total_days}, or from the hire date "
        "for a mid-month joiner.",
    ),
    (
        "Loss Of Pay",
        "loss_of_pay",
        _DAYS,
        "ELAPSED working days that were neither attended nor paid leave. Weekends, holidays "
        "and days still to come are never loss of pay.",
    ),
    ("Effective Paid Days", "effective_paid_days", _DAYS, "Base Days less Loss Of Pay."),
    (
        "Basic",
        "basic",
        _MONEY,
        "{basic_pct}% of monthly CTC, x Effective Paid Days / {total_days}.",
    ),
    ("House Rent Allowance", "hra", _MONEY, "{hra_pct}% of Basic, prorated the same way."),
    (
        "Fixed Allowance",
        "fixed_allowance",
        _MONEY,
        "Gross less Basic less HRA (the balancing figure), prorated.",
    ),
    (
        "Reimbursement",
        "reimbursement",
        _MONEY,
        "Always 0. Expenses are reported in Reimbursement Pay.",
    ),
    (
        "Total Reimbursements",
        "total_reimbursements",
        _MONEY,
        "Always 0. Expenses are reported in Reimbursement Pay.",
    ),
    (
        "Fixed Monthly Earnings",
        "fixed_monthly_earnings",
        _MONEY,
        "Basic + HRA + Fixed Allowance, i.e. the prorated Gross.",
    ),
    (
        "Fixed Monthly Costs (Earnings + Employer Contributions)",
        "fixed_monthly_costs",
        _MONEY,
        "Prorated CTC: the earnings above plus the employer PF carved out of CTC to reach them.",
    ),
    (
        "Total Earnings",
        "total_earnings",
        _MONEY,
        "Fixed Monthly Earnings + any earning adjustments posted for this month.",
    ),
    (
        "EPF Contribution",
        "epf_employee",
        _MONEY,
        "The employee half: {pf_pct}% of Basic capped at {pf_cap} a month, prorated. 0 for "
        "anyone whose compensation record has PF switched off.",
    ),
    (
        "EPF Contribution Employer",
        "epf_employer",
        _MONEY,
        "EPF Contribution less EPS Contribution Employer. The employer matches the employee "
        "half; this is what is left after the pension slice.",
    ),
    (
        "EPS Contribution Employer",
        "eps_employer",
        _MONEY,
        "{eps_rate}% of Basic counted up to {pf_ceiling}, and a flat {eps_cap} at or above it.",
    ),
    (
        "Employer EDLI Contribution Employer",
        "edli_employer",
        _MONEY,
        "{edli_rate}% of Basic counted up to {pf_ceiling}.",
    ),
    (
        "Employer EPF Admin Charges Employer",
        "epf_admin_employer",
        _MONEY,
        "{admin_rate}% of Basic. Uncapped, unlike EPS and EDLI.",
    ),
    (
        "Total Employer Contributions",
        "total_employer_contributions",
        _MONEY,
        "EPF Employer + EPS + EDLI + Admin Charges.",
    ),
    ("Income Tax", "income_tax", _MONEY, "{income_tax}"),
    ("Maharashtra Professional Tax", "professional_tax", _MONEY, "{professional_tax}"),
    (
        "Total Deductions",
        "total_deductions",
        _MONEY,
        "EPF Contribution + Income Tax + Professional Tax + any deduction adjustments posted "
        "for this month.",
    ),
    ("Gross Pay", "gross_pay", _MONEY, "Same figure as Total Earnings."),
    # The three pay columns HR reconciles against the bank. Kept in this order
    # and adjacent on purpose: `Total Pay` is the visible sum of the two before
    # it, so a reader can check the arithmetic without leaving the row.
    (
        "Net Monthly Total Pay",
        "net_monthly_total_pay",
        _MONEY,
        "Total Earnings less Total Deductions. SALARY ONLY: expense repayment is deliberately "
        "left out so this reconciles against the salary ledger.",
    ),
    (
        "Reimbursement Pay",
        "reimbursement_pay",
        _MONEY,
        "Approved expense claims repaid in this run. 0 when expenses are paid from their own "
        "run, so the money is never counted twice.",
    ),
    (
        "Total Pay",
        "total_pay",
        _MONEY,
        "Net Monthly Total Pay + Reimbursement Pay: what actually leaves the bank for this "
        "person. The sum of the two rounded columns, so it always adds up by eye.",
    ),
)

_HEADER_FILL = PatternFill("solid", fgColor="5A48E0")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def fmt_register_date(value: date | None) -> str:
    """Register date format (dd-mm-yyyy); empty string when unset."""
    return value.strftime("%d-%m-%Y") if value is not None else ""


@dataclass(frozen=True)
class PayrollExportRow:
    """One employee's line in the register. Money fields are whole currency units;
    day fields use a 30-day base. Dates are pre-formatted to strings so the sheet
    reads uniformly regardless of the viewer's locale."""

    period: str
    payroll_type: str
    employee_no: str
    employee_name: str
    department: str
    designation: str
    work_location: str
    date_of_joining: str
    date_of_birth: str
    last_working_day: str
    payment_mode: str
    account_holder: str
    bank_name: str
    account_number: str
    ifsc: str
    ctc_annual: int
    gross_annual: int
    base_days: float
    loss_of_pay: float
    effective_paid_days: float
    basic: int
    hra: int
    fixed_allowance: int
    reimbursement: int
    total_reimbursements: int
    fixed_monthly_earnings: int
    fixed_monthly_costs: int
    total_earnings: int
    epf_employee: int
    epf_employer: int
    eps_employer: int
    edli_employer: int
    epf_admin_employer: int
    total_employer_contributions: int
    income_tax: int
    professional_tax: int
    total_deductions: int
    gross_pay: int
    # Salary only - what the month's work earned, after deductions, with
    # expense repayment deliberately NOT folded in. `Net Pay` used to carry
    # this column's name while silently including reimbursements, so a month
    # with an expense claim made salary look higher than it was and the figure
    # could not be reconciled against the salary ledger.
    net_monthly_total_pay: int
    # Approved business expenses repaid in this run. Zero on a salary run whose
    # expenses are being paid separately, so the money is counted once.
    reimbursement_pay: int
    # What actually leaves the bank for this person: the two above, added.
    total_pay: int

    def as_cells(self) -> list[object]:
        return [getattr(self, name) for _header, name, _kind, _formula in _COLUMNS]


# Fail loudly if the dataclass and the column spec ever drift apart.
assert {name for _h, name, _k, _f in _COLUMNS} == {f.name for f in fields(PayrollExportRow)}


def _autofit(ws: Worksheet, rows: list[PayrollExportRow]) -> None:
    for idx, (header, name, _kind, _formula) in enumerate(_COLUMNS, start=1):
        widest = len(header)
        for row in rows:
            widest = max(widest, len(str(getattr(row, name))))
        ws.column_dimensions[get_column_letter(idx)].width = min(max(widest + 2, 10), 44)


_TITLE_FONT = Font(bold=True, size=14)
_SECTION_FONT = Font(bold=True, size=11, color="5A48E0")
_LABEL_FONT = Font(bold=True)

FORMULAS_SHEET = "Formulas"


def _rate_text(config: CalcConfig, *, total_days: int) -> dict[str, str]:
    """The placeholder values the column formulas are written against.

    Read off the month's ACTUAL settings rather than restated as literals, so the
    sheet can never explain a rate the register did not use.
    """
    pf_cap = config.pf_cap_minor // 100
    # The cap expressed as the Basic it stops at, which is how the PF rule is
    # written in the policy even though the stored knob is the amount.
    basic_ceiling = round(pf_cap * 100 / config.pf_pct) if config.pf_pct else 0
    income_tax = (
        "New regime: full-month Gross x 12, less the standard deduction, through the "
        "slabs, less the 87A rebate, divided back to a month. A flat monthly "
        "withholding - never prorated, because it is a tax on the year's salary."
        if config.deduct_income_tax
        else "Not withheld. Income tax is switched off in Payroll settings."
    )
    professional_tax = (
        f"{config.professional_tax_minor // 100:,} a month and "
        f"{config.professional_tax_feb_minor // 100:,} in February. Flat, never prorated. "
        f"NIL for a female employee whose full-month gross is "
        f"{config.professional_tax_exempt_upto_minor // 100:,} or less."
    )
    return {
        "basic_pct": str(config.basic_pct),
        "hra_pct": str(config.hra_pct),
        "pf_pct": str(config.pf_pct),
        "pf_cap": f"{pf_cap:,}",
        "pf_basic_ceiling": f"{basic_ceiling:,}",
        "pf_ceiling": f"{PF_WAGE_CEILING_MINOR // 100:,}",
        "eps_cap": f"{EPS_CAP_MINOR // 100:,}",
        "eps_rate": f"{EPS_RATE_PCT:g}",
        "edli_rate": f"{EDLI_RATE_PCT:g}",
        "admin_rate": f"{EPF_ADMIN_RATE_PCT:g}",
        "total_days": str(total_days),
        "income_tax": income_tax,
        "professional_tax": professional_tax,
    }


_PRORATION_NOTES = (
    "Earnings (Basic, HRA, Fixed Allowance, Gross, CTC) and BOTH PF figures scale by "
    "Effective Paid Days / {total_days}.",
    "Professional Tax and Income Tax do NOT scale. They are flat statutory charges on the "
    "month, so a short month is charged the same as a full one.",
    "Loss of pay is charged only on working days that have already ELAPSED. A weekend, a "
    "holiday, or a working day still ahead in the month is never counted as absence.",
    "Nobody paid for any day owes a deduction either: a zero-paid month is an all-zero row, "
    "never a negative net.",
    "Net is rebuilt from the prorated lines. It is never itself prorated, which would scale "
    "the flat charges along with it.",
)


def _write_formulas_sheet(
    wb: Workbook,
    *,
    config: CalcConfig,
    total_days: int,
    month_label: str,
    currency: str,
    reimbursement_only: bool,
) -> None:
    """A second sheet stating how every column on the register was derived.

    HR signs off a number they can re-derive; auditors ask the same question a
    year later, when whoever ran the month has moved on. Both used to be answered
    by asking an engineer to read the calculator.
    """
    ws = cast(Worksheet, wb.create_sheet(FORMULAS_SHEET))
    rates = _rate_text(config, total_days=total_days)

    row = 0

    def put(values: list[str], *, font: Font | None = None, wrap: bool = False) -> None:
        nonlocal row
        row += 1
        for idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=idx, value=value)
            if font is not None and idx == 1:
                cell.font = font
            if wrap and idx == len(values):
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    put([f"How this register is calculated - {month_label}"], font=_TITLE_FONT)
    put([f"All money is whole {currency}. The month has {total_days} calendar days."])
    if reimbursement_only:
        put(
            [
                "THIS IS A REIMBURSEMENT-ONLY RUN. Every salary column below is zero; only "
                "Reimbursement Pay and Total Pay carry money."
            ],
            font=_LABEL_FONT,
        )
    put([])

    put(["Rates in force this month"], font=_SECTION_FONT)
    for label, value in (
        ("Basic", f"{rates['basic_pct']}% of monthly CTC"),
        ("House Rent Allowance", f"{rates['hra_pct']}% of Basic"),
        ("Fixed Allowance", "Gross less Basic less HRA - the balancing figure"),
        (
            "Gross",
            "Monthly CTC less the employer PF share. The employer half comes OUT of CTC, "
            "not on top of it, which is why Gross is below CTC.",
        ),
        (
            "Provident Fund",
            f"{rates['pf_pct']}% of Basic, capped at {rates['pf_cap']} a month - that cap is "
            f"{rates['pf_pct']}% of a {rates['pf_basic_ceiling']} Basic. Employer matches it. "
            "Switchable off per employee on their compensation record.",
        ),
        (
            "Employer PF split",
            f"EPS {rates['eps_rate']}% and EDLI {rates['edli_rate']}% of Basic counted only up "
            f"to {rates['pf_ceiling']} (EPS a flat {rates['eps_cap']} at or above it); admin "
            f"charges {rates['admin_rate']}% of the full Basic; EPF employer is whatever the "
            "employee half leaves after EPS.",
        ),
        ("Professional Tax", rates["professional_tax"]),
        ("Income Tax", rates["income_tax"]),
    ):
        put([label, value], font=_LABEL_FONT)
    put([])

    put(["How attendance is applied"], font=_SECTION_FONT)
    for note in _PRORATION_NOTES:
        put(["", note.format_map(rates)])
    put([])

    put(["Column by column"], font=_SECTION_FONT)
    put(["Column", "Heading", "How it is calculated"], font=_LABEL_FONT)
    header_row = row
    for idx, (header, _name, _kind, formula) in enumerate(_COLUMNS, start=1):
        put([get_column_letter(idx), header, formula.format_map(rates)], wrap=True)

    for col in range(1, 4):
        cell = ws.cell(row=header_row, column=col)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 96
    ws.freeze_panes = f"A{header_row + 1}"


def build_payroll_xlsx(
    rows: list[PayrollExportRow],
    *,
    month_label: str,
    currency: str,
    config: CalcConfig,
    total_days: int,
    reimbursement_only: bool = False,
) -> bytes:
    """Render the month's payroll rows to .xlsx bytes in the register layout.

    Two sheets: the register itself, and a `Formulas` sheet stating how each of
    its columns was derived from the settings actually in force for `month_label`.
    """
    wb = Workbook()
    ws = cast(Worksheet, wb.active)  # a fresh Workbook always has an active sheet
    ws.title = "Payrun Employee Salary statemen"  # 31-char cap (matches the reference)

    ws.append([header for header, _name, _kind, _formula in _COLUMNS])
    for col in range(1, len(_COLUMNS) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row in rows:
        ws.append(row.as_cells())
        written = ws.max_row
        for col, (_header, _name, kind, _formula) in enumerate(_COLUMNS, start=1):
            if kind == _MONEY:
                ws.cell(row=written, column=col).number_format = "#,##0"
            elif kind == _DAYS:
                ws.cell(row=written, column=col).number_format = "0.00"

    ws.freeze_panes = "A2"
    _autofit(ws, rows)

    _write_formulas_sheet(
        wb,
        config=config,
        total_days=total_days,
        month_label=month_label,
        currency=currency,
        reimbursement_only=reimbursement_only,
    )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
