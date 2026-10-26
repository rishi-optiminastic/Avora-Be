"""Read the accounts team's bank payout file (xlsx or csv) into plain rows.

Accounts pays salaries from a spreadsheet - usually the 40-column "Payrun
Employee Salary statement" register Avora exports, sometimes a bank's own
bulk-payment report. Either way the file is untrusted input (Golden rule #1):

- The type is decided from the bytes, never from the filename or the browser's
  Content-Type (same reasoning as `app.core.uploads`).
- Headers vary ("Employee No" / "Emp ID", "Total Pay" / "Net Pay" / "Amount"),
  so each logical field accepts a list of aliases and the header row is found by
  scanning the first rows of every sheet.
- Size, the unzipped size of an xlsx, the rows scanned (blank ones included)
  and every amount are capped, so a crafted file cannot exhaust memory or
  overflow a BIGINT column. Any cell the reader chokes on becomes a
  row-numbered PayoutFileError (422), never a 500.
- A row whose amount is zero or negative is kept (it still identifies the
  person, so it is not "unmatched") but never counts as a payment: Avora's own
  register lists everyone, and a Net Pay can go negative when PT/TDS or a
  recovery is larger than a short month's pay.

Nothing here touches the database or logs cell values: a payout file carries
names, bank accounts and salaries.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

MAX_PAYOUT_FILE_BYTES = 5 * 1024 * 1024
MAX_PAYOUT_ROWS = 5000
# Rows read after the header, blank ones included: a sheet padded with empty
# rows (or a forged dimension) stops here instead of being walked to the end.
MAX_SCANNED_ROWS = 2 * MAX_PAYOUT_ROWS
# 10^12 minor units is ten billion rupees: far above any salary, far below
# BIGINT's limit, so a typo or a crafted cell is refused instead of overflowing.
MAX_AMOUNT_MINOR = 10**12
# An xlsx is a zip. The declared uncompressed size bounds what the reader can
# inflate, so a 5 MB zip bomb is refused before openpyxl opens it.
_MAX_XLSX_UNZIPPED_BYTES = 64 * 1024 * 1024
# Excel keeps 15 significant digits for a number; a longer account number typed
# into a General/Number cell has already lost its tail digits.
_EXCEL_NUMBER_DIGITS = 15
_WARNING_ROW_SAMPLE = 5
# How far down a sheet the header row may sit (banks put a title block on top).
_HEADER_SCAN_ROWS = 15
# Wider than any real payout layout (the register is 40); stops a crafted sheet
# from making every row a million cells long.
_MAX_COLUMNS = 200
_MAX_TEXT = 256
_SNIFF_BYTES = 4096
_XLSX_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .xls

_PAISE = Decimal(100)
_CENTS = Decimal("0.01")
_AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")
_CURRENCY_NOISE = re.compile(r"(inr|rs\.?|₹|,|\s)", re.IGNORECASE)
# The Indian "only" suffix: "Rs. 1,000/-".
_TRAILING_DASH = re.compile(r"/-\s*$")
_FAILED_STATUSES = frozenset(
    {"failed", "failure", "rejected", "returned", "reversed", "cancelled", "canceled", "bounced"}
)
_TOTAL_LABEL = "total"
# What a lazy sheet/CSV reader can raise on a malformed cell (e.g. an xlsx
# `<v>abc</v>` in a numeric cell is a ValueError from inside iter_rows).
_READ_ERRORS = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    ArithmeticError,
    EOFError,
    OSError,
    csv.Error,
    zipfile.BadZipFile,
)


class PayoutFileError(ValueError):
    """The file cannot be used. The message is safe to show the uploader."""


class PayoutFileType(StrEnum):
    XLSX = "xlsx"
    CSV = "csv"


class PayoutField(StrEnum):
    EMPLOYEE_NUMBER = "employee_number"
    ACCOUNT_NUMBER = "account_number"
    EMAIL = "email"
    NAME = "name"
    AMOUNT = "amount"
    REFERENCE = "reference"
    STATUS = "status"


IDENTIFIER_FIELDS = (PayoutField.EMPLOYEE_NUMBER, PayoutField.ACCOUNT_NUMBER, PayoutField.EMAIL)

# Normalised header (lowercase, alphanumerics only) -> field. Within a field the
# first alias present wins, so "Total Pay" beats "Net Pay" beats "Amount", and
# "Employee Name" beats "Account Holder Name".
_ALIASES: dict[PayoutField, tuple[str, ...]] = {
    PayoutField.EMPLOYEE_NUMBER: (
        "employeeno", "employeenumber", "empno", "employeeid", "empid", "employeecode",
        "empcode", "staffid", "staffno",
    ),
    PayoutField.ACCOUNT_NUMBER: (
        "accountnumber", "accountno", "bankaccountnumber", "bankaccountno",
        "beneficiaryaccountnumber", "beneficiaryaccountno", "creditaccountnumber",
        "acno", "acctno", "account",
    ),
    PayoutField.EMAIL: (
        "workemail", "email", "emailid", "emailaddress", "officialemail", "beneficiaryemail",
    ),
    PayoutField.NAME: (
        "employeename", "name", "payeename", "beneficiaryname", "accountholdername",
    ),
    PayoutField.AMOUNT: (
        # "totalpay" leads because it is what our own register calls the figure
        # that actually leaves the bank (salary + reimbursement). A proof file
        # built from that register carries all three pay columns, and matching
        # the salary-only one would under-count every row with an expense claim.
        "totalpay", "netmonthlytotalpay",
        "netpay", "amountpaid", "paidamount", "payoutamount", "transactionamount",
        "txnamount", "creditamount", "netamount", "netsalary", "netpayable", "amountinr",
        "amount",
    ),
    PayoutField.REFERENCE: (
        "utr", "utrno", "utrnumber", "bankreference", "referenceno", "referencenumber",
        "reference", "transactionid", "transactionreference", "paymentreference", "txnref",
    ),
    PayoutField.STATUS: ("paymentstatus", "transactionstatus", "txnstatus", "status"),
}  # fmt: skip


@dataclass(frozen=True)
class ParsedPayoutRow:
    row_number: int  # 1-based, as the uploader sees it in their spreadsheet
    employee_number: str | None
    account_number: str | None  # normalised; full number never leaves the service
    email: str | None
    payee_name: str | None
    amount_minor: int  # may be 0 or negative: such a row is never a payment
    reference: str | None
    bank_status: str | None
    failed: bool


@dataclass(frozen=True)
class ParsedPayoutFile:
    file_type: PayoutFileType
    fields: tuple[PayoutField, ...]  # the logical columns the header provided
    rows: tuple[ParsedPayoutRow, ...]
    # Safe-to-show notes about rows we read but could only partly use.
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Normalisers (shared with matching, so both sides compare the same way)
# --------------------------------------------------------------------------- #
def normalize_employee_number(value: str | None) -> str | None:
    """Make "014", "14" and 14.0 one employee number, and "EMP-14" the same as "EMP14"."""
    if not value:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if cleaned.isdigit():
        cleaned = cleaned.lstrip("0") or "0"
    return cleaned or None


def normalize_account_number(value: str | None) -> str | None:
    """Excel drops leading zeros from a numeric account number, so compare without them."""
    if not value:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z]", "", value).upper().lstrip("0")
    return cleaned or None


def normalize_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    return value.strip().lower()


def _normalize_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""


def _cell_text(value: object) -> str:
    """A cell as text. Whole floats lose their ".0" (Excel stores 14 as 14.0)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_amount_minor(value: object) -> int | None:
    """Money cell -> integer minor units (negative allowed). None when blank.

    PayoutFileError ("not a number" / "too large") when the cell is unusable.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise PayoutFileError("not a number")
    if isinstance(value, int | float | Decimal):
        amount = Decimal(str(value))
    else:
        text = _CURRENCY_NOISE.sub("", _TRAILING_DASH.sub("", str(value).strip()))
        if not _AMOUNT_RE.match(text):
            raise PayoutFileError("not a number")
        amount = Decimal(text)
    if not amount.is_finite():
        raise PayoutFileError("not a number")
    if abs(amount) * _PAISE > MAX_AMOUNT_MINOR:
        raise PayoutFileError("too large")
    try:
        return int((amount.quantize(_CENTS, rounding=ROUND_HALF_UP) * _PAISE).to_integral_value())
    except InvalidOperation as exc:
        raise PayoutFileError("not a number") from exc


# --------------------------------------------------------------------------- #
# File type + raw table reading
# --------------------------------------------------------------------------- #
def _decode_text(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def detect_payout_file_type(data: bytes) -> PayoutFileType | None:
    """xlsx (a zip) or csv (plain text, no NUL bytes); anything else is refused."""
    if data.startswith(_XLSX_MAGIC):
        return PayoutFileType.XLSX
    if not data or b"\x00" in data[:_SNIFF_BYTES] or data.startswith(_OLE_MAGIC):
        return None
    return PayoutFileType.CSV if _decode_text(data) is not None else None


_UNREADABLE_XLSX = "This spreadsheet could not be read. Save it as .xlsx and retry."


def _check_unzipped_size(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unzipped = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise PayoutFileError(_UNREADABLE_XLSX) from exc
    if unzipped > _MAX_XLSX_UNZIPPED_BYTES:
        raise PayoutFileError("This spreadsheet is too large once unpacked.")


def _xlsx_sheets(data: bytes) -> Iterator[Iterable[Sequence[object]]]:
    _check_unzipped_size(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError, ValueError) as exc:
        raise PayoutFileError(_UNREADABLE_XLSX) from exc
    try:
        for sheet in workbook.worksheets:
            yield sheet.iter_rows(values_only=True, max_col=_MAX_COLUMNS)
    finally:
        workbook.close()


def _csv_sheet(data: bytes) -> Iterable[Sequence[object]]:
    text = _decode_text(data)
    if text is None:
        raise PayoutFileError("This CSV is not readable text.")
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:_SNIFF_BYTES], delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel
    return csv.reader(io.StringIO(text), dialect)


# --------------------------------------------------------------------------- #
# Header + rows
# --------------------------------------------------------------------------- #
def _header_map(cells: Sequence[object]) -> dict[PayoutField, int] | None:
    """Column index per field, if this row looks like a payout header."""
    positions: dict[str, int] = {}
    for index, cell in enumerate(cells[:_MAX_COLUMNS]):
        positions.setdefault(_normalize_header(cell), index)
    mapping: dict[PayoutField, int] = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in positions:
                mapping[field] = positions[alias]
                break
    has_identifier = any(f in mapping for f in IDENTIFIER_FIELDS)
    return mapping if PayoutField.AMOUNT in mapping and has_identifier else None


def _text_at(cells: Sequence[object], mapping: dict[PayoutField, int], field: PayoutField) -> str:
    index = mapping.get(field)
    if index is None or index >= len(cells):
        return ""
    return _cell_text(cells[index])[:_MAX_TEXT]


def _is_total_label(text: str) -> bool:
    """A "Total", "Net Total", "Grand Total" or "Sub-total" row: the sheet's own sum."""
    return _TOTAL_LABEL in text.lower()


def _is_skippable(employee_number: str | None, name: str) -> bool:
    """A blank line, or the sheet's own total row (labelled in any text column)."""
    if employee_number:
        return _is_total_label(employee_number)
    return not name or _is_total_label(name)


def _lost_digits(cells: Sequence[object], mapping: dict[PayoutField, int]) -> bool:
    """An account number Excel stored as a float with more digits than it keeps.

    Excel rounds any number to 15 significant digits, so a 16+ digit account in a
    numeric cell is already wrong; matching on it would tie the line to nobody,
    or to the wrong person.
    """
    index = mapping.get(PayoutField.ACCOUNT_NUMBER)
    if index is None or index >= len(cells):
        return False
    value = cells[index]
    if not isinstance(value, float) or not value.is_integer():
        return False
    return len(str(int(abs(value)))) > _EXCEL_NUMBER_DIGITS


def _parse_row(
    cells: Sequence[object], mapping: dict[PayoutField, int], row_number: int
) -> ParsedPayoutRow | None:
    employee_number = _text_at(cells, mapping, PayoutField.EMPLOYEE_NUMBER) or None
    account_number = (
        None
        if _lost_digits(cells, mapping)
        else normalize_account_number(_text_at(cells, mapping, PayoutField.ACCOUNT_NUMBER))
    )
    email = normalize_email(_text_at(cells, mapping, PayoutField.EMAIL))
    name = _text_at(cells, mapping, PayoutField.NAME)
    amount_index = mapping[PayoutField.AMOUNT]
    raw_amount = cells[amount_index] if amount_index < len(cells) else None
    if not (account_number or email) and _is_skippable(employee_number, name):
        return None
    status = _text_at(cells, mapping, PayoutField.STATUS) or None
    failed = status is not None and status.lower() in _FAILED_STATUSES
    try:
        amount = parse_amount_minor(raw_amount)
    except PayoutFileError as exc:
        raise PayoutFileError(f"Row {row_number}: the amount is {exc}.") from exc
    if amount is None and not failed:
        raise PayoutFileError(f"Row {row_number}: the amount is missing.")
    return ParsedPayoutRow(
        row_number=row_number,
        employee_number=employee_number,
        account_number=account_number,
        email=email,
        payee_name=name or None,
        amount_minor=amount or 0,
        reference=_text_at(cells, mapping, PayoutField.REFERENCE)[:64] or None,
        bank_status=status[:32] if status else None,
        failed=failed,
    )


def _numbered(table: Iterable[Sequence[object]]) -> Iterator[tuple[int, Sequence[object]]]:
    """(1-based row number, cells), turning any reader failure into a PayoutFileError.

    openpyxl converts cells lazily, so a garbled cell surfaces as a ValueError
    from inside the iteration, not from load_workbook.
    """
    rows = iter(table)
    row_number = 0
    while True:
        row_number += 1
        try:
            cells = next(rows)
        except StopIteration:
            return
        except PayoutFileError:
            raise
        except _READ_ERRORS as exc:
            raise PayoutFileError(
                f"Row {row_number}: this row could not be read. Check its cells and retry."
            ) from exc
        yield row_number, cells


@dataclass
class _Table:
    mapping: dict[PayoutField, int]
    rows: list[ParsedPayoutRow]
    lost_digit_rows: list[int]


def _parse_table(table: Iterable[Sequence[object]]) -> _Table | None:
    found: _Table | None = None
    header_row = 0
    for row_number, cells in _numbered(table):
        if found is None:
            if row_number > _HEADER_SCAN_ROWS:
                return None
            mapping = _header_map(cells)
            if mapping is not None:
                found, header_row = _Table(mapping, [], []), row_number
            continue
        if row_number - header_row > MAX_SCANNED_ROWS:
            raise PayoutFileError(
                f"The file has more than {MAX_SCANNED_ROWS} rows under its header. "
                "Remove empty or unrelated rows and retry."
            )
        _take_row(found, cells, row_number)
    return found


def _take_row(table: _Table, cells: Sequence[object], row_number: int) -> None:
    try:
        parsed = _parse_row(cells, table.mapping, row_number)
    except PayoutFileError:
        raise
    except _READ_ERRORS as exc:
        raise PayoutFileError(f"Row {row_number}: this row could not be read.") from exc
    if parsed is None:
        return
    if len(table.rows) >= MAX_PAYOUT_ROWS:
        raise PayoutFileError(f"The file has more than {MAX_PAYOUT_ROWS} payment rows.")
    if _lost_digits(cells, table.mapping):
        table.lost_digit_rows.append(row_number)
    table.rows.append(parsed)


def _lost_digits_warning(row_numbers: Sequence[int]) -> str:
    sample = ", ".join(str(n) for n in row_numbers[:_WARNING_ROW_SAMPLE])
    extra = len(row_numbers) - _WARNING_ROW_SAMPLE
    more = f" and {extra} more" if extra > 0 else ""
    label = "Row" if len(row_numbers) == 1 else "Rows"
    return (
        f"{label} {sample}{more}: the account number has more than {_EXCEL_NUMBER_DIGITS} digits "
        "and Excel stored it as a number, which drops digits, so it was not used for "
        "matching or the account check. Format the account column as text in Excel."
    )


def parse_payout_file(data: bytes) -> ParsedPayoutFile:
    """Parse an uploaded payout file, or raise PayoutFileError saying why not."""
    if len(data) > MAX_PAYOUT_FILE_BYTES:
        raise PayoutFileError("The file is larger than 5 MB.")
    file_type = detect_payout_file_type(data)
    if file_type is None:
        raise PayoutFileError("Upload the payout file as .xlsx or .csv.")
    sheets = _xlsx_sheets(data) if file_type is PayoutFileType.XLSX else iter([_csv_sheet(data)])
    for table in sheets:
        parsed = _parse_table(table)
        if parsed is None:
            continue
        if not parsed.rows:
            raise PayoutFileError("The file has a header row but no payments.")
        warnings = (_lost_digits_warning(parsed.lost_digit_rows),) if parsed.lost_digit_rows else ()
        return ParsedPayoutFile(
            file_type=file_type,
            fields=tuple(field for field in PayoutField if field in parsed.mapping),
            rows=tuple(parsed.rows),
            warnings=warnings,
        )
    raise PayoutFileError(
        "No payout header found. The file needs an amount column (e.g. Total Pay) and "
        "Employee No, Account Number or Email."
    )
