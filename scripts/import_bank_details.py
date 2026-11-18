"""Bulk-load salary bank details from a CSV export of the bank sheet.

The sheet HR keeps has the columns ``IFSC Code``, ``Account Number``,
``Account Holder Name`` and ``Bank Account`` (the bank's name). An optional
``Email`` column pins a row to one person when the name alone is ambiguous.

Each row is matched to an ACTIVE, non-guest employee of the workspace by name:
every word of the employee's Avora name must appear in the account holder
name, which tolerates the father's/middle name banks print
("SUPRIYA SHANKAR YENPURE" matches "Supriya Yenpure"). A row that matches
nobody, or more than one person, is reported and skipped - never guessed.

This is the corporate salary account payroll pays into. It is Avora's own, so
Circle-linked people are included: Circle keeps their personal account and the
Circle sync never touches bank details here.

Dry run by default. Nothing is written until ``--apply`` is passed. Account
numbers are encrypted exactly as the Bank details form does it, the change is
audited per person, and the output only ever shows the last four digits.

    uv run python -m scripts.import_bank_details bank.csv              # preview
    uv run python -m scripts.import_bank_details bank.csv --apply      # write
    uv run python -m scripts.import_bank_details bank.csv --workspace acme
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.pii_crypto import encrypt_pii
from app.core.tenancy import use_schema, use_shared_only, validate_schema_name
from app.db.session import SessionFactory, engine
from app.models.compensation import AccountType
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.workspace import WorkspaceRepository, normalize_email
from app.schemas.compensation import BankDetailsWrite

DEFAULT_WORKSPACE = "optiminastic"
ACTOR = "bank-details-import"
VISIBLE_DIGITS = 4

COLUMN_IFSC = "ifsc code"
COLUMN_ACCOUNT = "account number"
COLUMN_HOLDER = "account holder name"
COLUMN_BANK = "bank account"
COLUMN_EMAIL = "email"
REQUIRED_COLUMNS = (COLUMN_IFSC, COLUMN_ACCOUNT, COLUMN_HOLDER, COLUMN_BANK)

# Spreadsheets mark a number as text with a quote; it is not part of the value.
_TEXT_MARKERS = "'\u2018\u2019`"
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class BankRow:
    line: int
    holder: str
    details: BankDetailsWrite
    email: str | None


@dataclass(frozen=True)
class Person:
    id: object
    name: str
    email: str


def mask(account_number: str | None) -> str:
    if not account_number:
        return "-"
    return f"****{account_number[-VISIBLE_DIGITS:]}"


def name_words(name: str) -> frozenset[str]:
    return frozenset(_WORD.findall(name.casefold()))


def clean_cell(value: str | None) -> str:
    return (value or "").strip().strip(_TEXT_MARKERS).strip()


def parse_rows(lines: Iterable[str]) -> tuple[list[BankRow], list[str]]:
    """The usable rows, and a sentence for every row that could not be read."""
    reader = csv.DictReader(lines)
    headers = {(h or "").strip().casefold(): h for h in reader.fieldnames or []}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], [f"missing column(s): {', '.join(missing)}"]

    rows: list[BankRow] = []
    problems: list[str] = []
    for line, raw in enumerate(reader, start=2):
        cell = {key: clean_cell(raw.get(original)) for key, original in headers.items()}
        holder = cell[COLUMN_HOLDER]
        if not holder and not cell[COLUMN_ACCOUNT]:
            continue  # a blank line at the bottom of the sheet
        try:
            details = BankDetailsWrite(
                account_holder_name=holder,
                bank_name=cell[COLUMN_BANK],
                account_number=cell[COLUMN_ACCOUNT],
                ifsc_code=cell[COLUMN_IFSC],
            )
        except ValidationError as err:
            reasons = "; ".join(e["msg"] for e in err.errors())
            problems.append(f"line {line} ({holder or 'no name'}): {reasons}")
            continue
        rows.append(
            BankRow(line, holder, details, normalize_email(cell.get(COLUMN_EMAIL) or "") or None)
        )
    return rows, problems


def match(row: BankRow, people: Sequence[Person]) -> list[Person]:
    """Everyone this row could belong to. Exactly one is a match."""
    if row.email:
        return [p for p in people if normalize_email(p.email) == row.email]
    holder = name_words(row.holder)
    return [p for p in people if (words := name_words(p.name)) and words <= holder]


async def _people(session) -> list[Person]:  # type: ignore[no-untyped-def]
    result = await session.execute(
        select(Employee).where(Employee.is_active.is_(True), Employee.is_guest.is_(False))
    )
    return [Person(e.id, e.full_name, e.work_email) for e in result.scalars().all()]


async def _schema_for(session, slug: str) -> str:  # type: ignore[no-untyped-def]
    await use_shared_only(session)
    workspace = await WorkspaceRepository(session).get_by_slug(slug)
    if workspace is None:
        raise SystemExit(f"no workspace with slug {slug!r}")
    return validate_schema_name(workspace.schema_name)


async def run(path: Path, slug: str, account_type: AccountType, apply: bool) -> int:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows, problems = parse_rows(handle)
    for problem in problems:
        print(f"SKIP  {problem}")

    settings = get_settings()
    written = 0
    async with SessionFactory() as session:
        await use_schema(session, await _schema_for(session, slug))
        people = await _people(session)
        compensation = CompensationRepository(session)
        audit = AuditRepository(session)
        claimed: dict[object, int] = {}

        for row in rows:
            found = match(row, people)
            account = row.details.account_number
            if len(found) != 1:
                why = "matches nobody" if not found else f"matches {len(found)} people"
                print(
                    f"SKIP  line {row.line} {row.holder} {mask(account)}: {why}"
                    " (add an Email column)"
                )
                continue
            person = found[0]
            if person.id in claimed:
                print(
                    f"SKIP  line {row.line} {person.name}: already set by line {claimed[person.id]}"
                )
                continue
            claimed[person.id] = row.line

            current = await compensation.get_for_employee(person.id)  # type: ignore[arg-type]
            print(
                f"{'SET ' if apply else 'WOULD'} {person.name} <- {row.holder}"
                f" {mask(account)} {row.details.ifsc_code}"
            )
            if not apply:
                continue
            await compensation.upsert_bank(
                person.id,  # type: ignore[arg-type]
                account_holder_name=row.details.account_holder_name,
                bank_name=row.details.bank_name,
                account_number_encrypted=encrypt_pii(settings, account) if account else None,
                ifsc_code=row.details.ifsc_code,
                account_type=(
                    current.account_type if current and current.account_type else account_type
                ),
                updated_by=None,  # type: ignore[arg-type]
            )
            await audit.append(
                actor=ACTOR, action="compensation.bank.update", target=f"employee:{person.id}"
            )
            written += 1

        if apply:
            await session.commit()
    await engine.dispose()

    print(
        f"\n{len(claimed)} matched, {written} written"
        + ("" if apply else " (dry run: pass --apply to save)")
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv", type=Path, help="CSV export of the bank sheet")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="workspace slug")
    parser.add_argument(
        "--account-type",
        type=AccountType,
        default=AccountType.SAVINGS,
        help="used only for people with no account type yet",
    )
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    if not args.csv.is_file():
        raise SystemExit(f"no such file: {args.csv}")
    sys.exit(asyncio.run(run(args.csv, args.workspace, args.account_type, args.apply)))


if __name__ == "__main__":
    main()
