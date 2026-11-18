"""The bank-sheet import: reading the export and matching rows to people."""

from scripts.import_bank_details import Person, mask, match, parse_rows

HEADER = "IFSC Code,Account Number,Account Holder Name,Bank Account\n"


def _person(name: str, email: str = "") -> Person:
    return Person(id=name, name=name, email=email or f"{name.split()[0].lower()}@x.com")


def test_strips_the_spreadsheet_text_marker_and_keeps_leading_zero() -> None:
    rows, problems = parse_rows(
        [HEADER, "kkbk0000958,0123456789',ASHA RAM PATIL,Kotak Mahindra Bank\n"]
    )
    assert problems == []
    assert rows[0].details.account_number == "0123456789"
    assert rows[0].details.ifsc_code == "KKBK0000958"


def test_bad_rows_are_reported_not_imported() -> None:
    rows, problems = parse_rows([HEADER, "KKBK0000958,12AB,Asha Patil,Kotak\n", ",,,\n"])
    assert rows == []
    assert len(problems) == 1 and "line 2" in problems[0]


def test_missing_column_is_refused() -> None:
    rows, problems = parse_rows(["IFSC Code,Account Number\n", "KKBK0000958,0123456789\n"])
    assert rows == [] and "account holder name" in problems[0]


def test_holder_name_with_middle_name_matches_the_avora_name() -> None:
    rows, _ = parse_rows([HEADER, "KKBK0000958,0123456789,ASHA RAM PATIL,Kotak\n"])
    people = [_person("Asha Patil"), _person("Asha Shah")]
    assert [p.name for p in match(rows[0], people)] == ["Asha Patil"]


def test_ambiguous_name_matches_both_so_it_is_skipped() -> None:
    rows, _ = parse_rows([HEADER, "KKBK0000958,0123456789,Asha Patil,Kotak\n"])
    assert len(match(rows[0], [_person("Asha"), _person("Asha Patil")])) == 2


def test_email_column_pins_the_row() -> None:
    sheet = "IFSC Code,Account Number,Account Holder Name,Bank Account,Email\n"
    rows, _ = parse_rows([sheet, "KKBK0000958,0123456789,Asha Patil,Kotak,ASHA@X.com\n"])
    people = [_person("Asha", "asha@x.com"), _person("Asha Patil", "ap@x.com")]
    assert [p.name for p in match(rows[0], people)] == ["Asha"]


def test_mask_shows_only_the_last_four() -> None:
    assert mask("0123456789") == "****6789"
