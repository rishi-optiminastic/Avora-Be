"""Payout file parsing and the pure reconciliation rules.

All data is synthetic. The parser must cope with the 40-column register Avora
exports and with a bank's own report, in xlsx or csv, without trusting the
filename or the header spelling.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from openpyxl import Workbook

from app.core import payout_file
from app.core.payout_file import (
    MAX_AMOUNT_MINOR,
    MAX_PAYOUT_FILE_BYTES,
    MAX_SCANNED_ROWS,
    ParsedPayoutRow,
    PayoutField,
    PayoutFileError,
    PayoutFileType,
    detect_payout_file_type,
    parse_amount_minor,
    parse_payout_file,
)
from app.models.payout_verification import PayoutMismatchKind
from app.services.payout_reconcile import (
    EmployeeIndex,
    Identity,
    PaidLine,
    reconcile,
    register_minor,
)


def _xlsx(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_register_style_csv() -> None:
    data = (
        b"Period,Employee No,Employee Name,Account Number,Net Pay\n"
        b"April 2026,7,Test Person,000123456789,50000\n"
        b"April 2026,8,Other Person,987654321,40000.5\n"
    )
    parsed = parse_payout_file(data)
    assert parsed.file_type is PayoutFileType.CSV
    assert PayoutField.EMPLOYEE_NUMBER in parsed.fields
    assert PayoutField.ACCOUNT_NUMBER in parsed.fields
    first, second = parsed.rows
    assert first.row_number == 2
    assert first.employee_number == "7"
    assert first.account_number == "123456789"  # leading zeros dropped
    assert first.amount_minor == 5_000_000
    assert second.amount_minor == 4_000_050


def test_bank_style_csv_with_bom_semicolons_and_status() -> None:
    data = (
        "﻿Emp ID;Beneficiary Account No;Beneficiary Name;Amount Paid;UTR No;Status\n"
        "EMP-07;123456789;Test Person;₹ 1,234.50;UTR0001;Success\n"
        "EMP-08;555;Other Person;;UTR0002;Failed\n"
    ).encode()
    parsed = parse_payout_file(data)
    ok, failed = parsed.rows
    assert ok.employee_number == "EMP-07"
    assert ok.amount_minor == 123_450
    assert ok.reference == "UTR0001"
    assert ok.failed is False
    assert failed.failed is True
    assert failed.amount_minor == 0  # a failed line may carry no amount
    assert PayoutField.REFERENCE in parsed.fields


def test_xlsx_with_title_rows_numeric_cells_and_total_row() -> None:
    data = _xlsx(
        [
            ["Salary payout - synthetic"],
            [],
            ["Employee Code", "Work Email", "Net Amount", "Reference Number"],
            [7.0, "A.Person@Corp.Test", 50000, "R1"],
            [None, None, None, None],
            ["", "", 50000, ""],
            ["Total", "", 100000, ""],
        ]
    )
    parsed = parse_payout_file(data)
    assert parsed.file_type is PayoutFileType.XLSX
    assert len(parsed.rows) == 1  # blank, unnamed and total rows skipped
    (row,) = parsed.rows
    assert row.row_number == 4
    assert row.employee_number == "7"
    assert row.email == "a.person@corp.test"
    assert row.amount_minor == 5_000_000


def test_net_pay_beats_other_amount_columns() -> None:
    data = b"Employee No,Gross Pay,Net Pay,Amount\n7,90000,50000,1\n"
    (row,) = parse_payout_file(data).rows
    assert row.amount_minor == 5_000_000


def test_unidentified_named_row_is_kept_as_unmatched_material() -> None:
    data = b"Employee No,Employee Name,Net Pay\n,Someone Unknown,100\n"
    (row,) = parse_payout_file(data).rows
    assert row.employee_number is None
    assert row.payee_name == "Someone Unknown"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"%PDF-1.7 not a payout", "header"),
        (b"\xd0\xcf\x11\xe0legacy-xls", ".xlsx or .csv"),
        (b"bin\x00ary", ".xlsx or .csv"),
        (b"", ".xlsx or .csv"),
        (b"Name,Comment\nA,B\n", "header"),
        (b"Employee No,Net Pay\n", "no payments"),
        (b"Employee No,Net Pay\n7,abc\n", "Row 2"),
        (b"Employee No,Net Pay\n7,99999999999\n", "Row 2: the amount is too large"),
        (b"Employee No,Net Pay\n7,\n", "missing"),
        (b"PK\x03\x04garbage", "could not be read"),
    ],
)
def test_unusable_files_are_refused_with_a_reason(data: bytes, message: str) -> None:
    with pytest.raises(PayoutFileError) as caught:
        parse_payout_file(data)
    assert message in str(caught.value)


def test_oversized_file_is_refused() -> None:
    with pytest.raises(PayoutFileError, match="5 MB"):
        parse_payout_file(b"a" * (MAX_PAYOUT_FILE_BYTES + 1))


def test_type_is_sniffed_from_bytes() -> None:
    assert detect_payout_file_type(_xlsx([["a"]])) is PayoutFileType.XLSX
    assert detect_payout_file_type(b"a,b\n1,2\n") is PayoutFileType.CSV
    assert detect_payout_file_type(b"\x89PNG\r\n\x1a\n\x00\x00") is None


@pytest.mark.parametrize(
    ("value", "minor"),
    [
        (50000, 5_000_000),
        (1.5, 150),
        ("Rs. 2,000", 200_000),
        ("INR 10.05", 1005),
        ("Rs. 1,000/-", 100_000),
        ("1,23,456.50", 12_345_650),
        ("₹ 1,000", 100_000),
        ("INR 1000", 100_000),
        ("-1,250", -125_000),
        (-300, -30_000),
        (0, 0),
        (None, None),
    ],
)
def test_amount_parsing(value: object, minor: int | None) -> None:
    assert parse_amount_minor(value) == minor


@pytest.mark.parametrize("value", [10**11, "1,00,00,00,00,001", float("inf"), -(10**11)])
def test_absurd_amounts_are_refused(value: object) -> None:
    with pytest.raises(PayoutFileError, match=r"too large|not a number"):
        parse_amount_minor(value)
    assert parse_amount_minor(MAX_AMOUNT_MINOR // 100) == MAX_AMOUNT_MINOR


def test_zero_and_negative_net_pay_rows_are_read_not_rejected() -> None:
    data = b"Employee No,Employee Name,Net Pay\n7,On Leave,0\n8,Recovery,-1200\n9,Paid,500\n"
    rows = parse_payout_file(data).rows
    assert [r.amount_minor for r in rows] == [0, -120_000, 50_000]


def test_any_total_row_is_skipped() -> None:
    data = (
        b"Employee No,Employee Name,Net Pay\n7,A Person,100\n"
        b",Net Total,100\n,Grand Total,100\nTOTAL (April),,100\n"
    )
    assert [r.row_number for r in parse_payout_file(data).rows] == [2]


def test_scanned_rows_are_capped_including_blank_ones() -> None:
    data = b"Employee No,Net Pay\n7,1\n" + b"\n" * (MAX_SCANNED_ROWS + 1)
    with pytest.raises(PayoutFileError, match="rows under its header"):
        parse_payout_file(data)


def _rewrite_sheet(data: bytes, old: bytes, new: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                assert old in body
                body = body.replace(old, new)
            target.writestr(info, body)
    return out.getvalue()


def test_malformed_xlsx_number_cell_is_a_row_numbered_error() -> None:
    data = _xlsx([["Employee No", "Net Pay"], [7, 500], [8, 777]])
    broken = _rewrite_sheet(data, b"<v>777</v>", b"<v>abc</v>")
    with pytest.raises(PayoutFileError, match="Row 3"):
        parse_payout_file(broken)


def test_xlsx_that_unzips_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payout_file, "_MAX_XLSX_UNZIPPED_BYTES", 1000)
    with pytest.raises(PayoutFileError, match="too large once unpacked"):
        parse_payout_file(_xlsx([["Employee No", "Net Pay"], [7, 500]]))


def test_excel_rounded_account_number_is_dropped_with_a_warning() -> None:
    data = _xlsx(
        [
            ["Employee No", "Account Number", "Net Pay"],
            [7, 123456789012345678.0, 500],  # 18 digits: Excel already lost the tail
            [8, 123456789012345.0, 500],  # 15 digits survive a numeric cell
            [9, "1234567890123456789", 500],  # text keeps every digit
        ]
    )
    parsed = parse_payout_file(data)
    assert [r.account_number for r in parsed.rows] == [
        None,
        "123456789012345",
        "1234567890123456789",
    ]
    (warning,) = parsed.warnings
    assert warning.startswith("Row 2:") and "as text in Excel" in warning


# --------------------------------------------------------------------------- #
# Matching + reconciliation
# --------------------------------------------------------------------------- #
def _row(**kw: object) -> ParsedPayoutRow:
    base: dict[str, object] = {
        "row_number": 2,
        "employee_number": None,
        "account_number": None,
        "email": None,
        "payee_name": None,
        "amount_minor": 100,
        "reference": None,
        "bank_status": None,
        "failed": False,
    }
    base.update(kw)
    return ParsedPayoutRow(**base)  # type: ignore[arg-type]


def test_matching_prefers_employee_number_and_skips_ambiguous_keys() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    index = EmployeeIndex.build(
        [
            Identity(a, "007", "a@corp.test", "0001234"),
            Identity(b, "8", "b@corp.test", "1234"),  # shares a's account (ambiguous)
        ]
    )
    assert index.match(_row(employee_number="7")) == (a, "employee_number")
    assert index.match(_row(account_number="1234")) == (None, None)
    assert index.match(_row(account_number="1234", email="b@corp.test")) == (b, "email")
    assert index.match(_row(employee_number="99")) == (None, None)


def test_account_check_compares_against_the_account_on_file() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    index = EmployeeIndex.build(
        [Identity(a, "7", "a@corp.test", "000123456789"), Identity(b, "8", None, None)]
    )
    row = _row(employee_number="7", account_number="999988887777")
    assert index.account_differs(row, a) is True
    same = _row(employee_number="7", account_number="123456789")  # leading zeros ignored
    assert index.account_differs(same, a) is False
    assert index.account_differs(_row(employee_number="7"), a) is False  # no column
    assert index.account_differs(_row(account_number="55"), b) is False  # none on file
    assert index.account_differs(row, None) is False


def test_each_mismatch_kind() -> None:
    ok, short, dup, unpaid, extra, moved = (uuid.uuid4() for _ in range(6))
    expected = {ok: 1000, short: 2000, dup: 3000, unpaid: 4000, moved: 500}
    lines = [
        PaidLine(0, ok, 1000, False),
        PaidLine(1, short, 1900, False),
        PaidLine(2, dup, 3000, False),
        PaidLine(3, dup, 3000, False),
        PaidLine(4, extra, 500, False),
        PaidLine(5, None, 700, False),
        PaidLine(6, unpaid, 4000, True),  # bank failed it: not paid
        PaidLine(7, moved, 500, False, account_differs=True),
    ]
    drafts = {d.kind: d for d in reconcile(expected, lines)}
    assert set(drafts) == set(PayoutMismatchKind)
    assert drafts[PayoutMismatchKind.ACCOUNT_MISMATCH].employee_id == moved
    assert drafts[PayoutMismatchKind.ACCOUNT_MISMATCH].row_index == 7
    assert drafts[PayoutMismatchKind.AMOUNT_MISMATCH].employee_id == short
    assert drafts[PayoutMismatchKind.AMOUNT_MISMATCH].paid_minor == 1900
    assert drafts[PayoutMismatchKind.DUPLICATE_PAYMENT].paid_minor == 6000
    assert drafts[PayoutMismatchKind.PAID_NOT_EXPECTED].employee_id == extra
    assert drafts[PayoutMismatchKind.EXPECTED_NOT_PAID].employee_id == unpaid
    assert drafts[PayoutMismatchKind.UNMATCHED_ROW].row_index == 5


def test_clean_payout_has_no_mismatches() -> None:
    person = uuid.uuid4()
    assert reconcile({person: 1000}, [PaidLine(0, person, 1000, False)]) == []


def test_zero_and_negative_rows_are_not_payments() -> None:
    nothing_due, due, unknown = uuid.uuid4(), uuid.uuid4(), None
    lines = [
        PaidLine(0, nothing_due, 0, False),  # the register lists everyone, Net Pay 0
        PaidLine(1, due, -120_000, False),  # PT/TDS larger than a short month's pay
        PaidLine(2, unknown, 0, False),  # not a payment, so not "unmatched" either
        PaidLine(3, nothing_due, -5, False, account_differs=True),  # no money moved
    ]
    drafts = reconcile({due: 50_000}, lines)
    assert [(d.kind, d.employee_id) for d in drafts] == [
        (PayoutMismatchKind.EXPECTED_NOT_PAID, due)
    ]


def test_register_minor_matches_the_export_rounding() -> None:
    assert register_minor(4_000_040) == 4_000_000
    assert register_minor(4_000_060) == 4_000_100
