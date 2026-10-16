"""Pure payout reconciliation: who a payment line is, and what does not add up.

No database and no I/O, so every mismatch rule is unit-testable on its own. The
service loads the employees and the expected amounts, then calls in here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.core.payout_file import (
    ParsedPayoutRow,
    normalize_account_number,
    normalize_email,
    normalize_employee_number,
)
from app.models.payout_verification import PayoutMismatchKind


def register_minor(minor: int) -> int:
    """The amount a payroll register row carries, back in minor units.

    The xlsx exports write whole rupees (`round(minor / 100)` in
    `PayrollService._export_row` / `_reimbursement_row`), so accounts pays whole
    rupees. Expecting the unrounded paise would flag every row; expecting what
    the register said makes the exact comparison meaningful.
    """
    return round(minor / 100) * 100


@dataclass(frozen=True)
class Identity:
    """What a payout line can be matched on, for one employee."""

    employee_id: uuid.UUID
    employee_number: str | None
    work_email: str | None
    account_number: str | None  # decrypted by the service, never persisted from here


@dataclass
class EmployeeIndex:
    """Lookups from a normalised identifier to the employees carrying it."""

    by_number: dict[str, set[uuid.UUID]] = field(default_factory=lambda: defaultdict(set))
    by_account: dict[str, set[uuid.UUID]] = field(default_factory=lambda: defaultdict(set))
    by_email: dict[str, set[uuid.UUID]] = field(default_factory=lambda: defaultdict(set))
    # The normalised account each employee has on file, for the account check.
    accounts: dict[uuid.UUID, str] = field(default_factory=dict)

    @classmethod
    def build(cls, identities: Iterable[Identity]) -> EmployeeIndex:
        index = cls()
        for person in identities:
            number = normalize_employee_number(person.employee_number)
            account = normalize_account_number(person.account_number)
            email = normalize_email(person.work_email)
            if number:
                index.by_number[number].add(person.employee_id)
            if account:
                index.by_account[account].add(person.employee_id)
                index.accounts[person.employee_id] = account
            if email:
                index.by_email[email].add(person.employee_id)
        return index

    def match(self, row: ParsedPayoutRow) -> tuple[uuid.UUID | None, str | None]:
        """(employee, which identifier matched). Employee number is the strongest
        key, then the bank account, then email. A key two employees share is
        ambiguous and skipped rather than guessed."""
        candidates = (
            ("employee_number", self.by_number, normalize_employee_number(row.employee_number)),
            ("account_number", self.by_account, row.account_number),
            ("email", self.by_email, row.email),
        )
        for label, lookup, key in candidates:
            if not key:
                continue
            found = lookup.get(key, set())
            if len(found) == 1:
                return next(iter(found)), label
        return None, None

    def account_differs(self, row: ParsedPayoutRow, employee_id: uuid.UUID | None) -> bool:
        """The line names this employee but pays a different account than on file.

        Only meaningful when the line was tied to the person by something other
        than the account (employee number or email) - a line matched BY account
        agrees by definition. Both sides are normalised (leading zeros ignored).
        Nothing to compare when either side has no account.
        """
        if employee_id is None or not row.account_number:
            return False
        on_file = self.accounts.get(employee_id)
        return on_file is not None and on_file != row.account_number


@dataclass(frozen=True)
class PaidLine:
    """One payout row after matching. `index` is its position in the upload."""

    index: int
    employee_id: uuid.UUID | None
    amount_minor: int
    failed: bool
    account_differs: bool = False

    @property
    def pays(self) -> bool:
        """Money actually moved: not failed at the bank, and a positive amount.

        A zero or negative Net Pay row (the register lists everyone) identifies
        the person but is not a payment.
        """
        return not self.failed and self.amount_minor > 0


@dataclass(frozen=True)
class MismatchDraft:
    kind: PayoutMismatchKind
    employee_id: uuid.UUID | None
    row_index: int | None
    expected_minor: int | None
    paid_minor: int | None


def _unmatched(lines: Sequence[PaidLine]) -> list[MismatchDraft]:
    return [
        MismatchDraft(PayoutMismatchKind.UNMATCHED_ROW, None, line.index, None, line.amount_minor)
        for line in lines
        if line.employee_id is None and line.pays
    ]


def _paid_by_employee(lines: Sequence[PaidLine]) -> dict[uuid.UUID, list[PaidLine]]:
    grouped: dict[uuid.UUID, list[PaidLine]] = defaultdict(list)
    for line in lines:
        if line.employee_id is not None and line.pays:
            grouped[line.employee_id].append(line)
    return grouped


def _account_mismatches(
    expected: Mapping[uuid.UUID, int], lines: Sequence[PaidLine]
) -> list[MismatchDraft]:
    return [
        MismatchDraft(
            PayoutMismatchKind.ACCOUNT_MISMATCH,
            line.employee_id,
            line.index,
            expected.get(line.employee_id),
            line.amount_minor,
        )
        for line in lines
        if line.employee_id is not None and line.pays and line.account_differs
    ]


def _employee_mismatch(
    employee_id: uuid.UUID, paid: list[PaidLine], expected: int | None
) -> MismatchDraft | None:
    total = sum(line.amount_minor for line in paid)
    first = paid[0].index
    if expected is None:
        return MismatchDraft(PayoutMismatchKind.PAID_NOT_EXPECTED, employee_id, first, None, total)
    if len(paid) > 1:
        return MismatchDraft(
            PayoutMismatchKind.DUPLICATE_PAYMENT, employee_id, first, expected, total
        )
    if total != expected:
        return MismatchDraft(
            PayoutMismatchKind.AMOUNT_MISMATCH, employee_id, first, expected, total
        )
    return None


def reconcile(expected: Mapping[uuid.UUID, int], lines: Sequence[PaidLine]) -> list[MismatchDraft]:
    """Every way the payout file disagrees with what payroll expected.

    - UNMATCHED_ROW: a payment we cannot tie to anyone.
    - PAID_NOT_EXPECTED: someone was paid who had nothing due this run.
    - DUPLICATE_PAYMENT: more than one payment to the same person.
    - AMOUNT_MISMATCH: one payment, but not the expected amount (exact minor units).
    - EXPECTED_NOT_PAID: someone due money has no successful payment line.
    - ACCOUNT_MISMATCH: a payment to a known person, but into a different bank
      account than the one on file (flagged on top of any amount finding).

    Lines the bank marked failed, and zero or negative amounts, never count as
    paid - and are not "unmatched" either, since no money went anywhere.
    """
    drafts = _unmatched(lines)
    drafts.extend(_account_mismatches(expected, lines))
    paid = _paid_by_employee(lines)
    for employee_id, employee_lines in paid.items():
        draft = _employee_mismatch(employee_id, employee_lines, expected.get(employee_id))
        if draft is not None:
            drafts.append(draft)
    for employee_id, amount in expected.items():
        if employee_id not in paid:
            drafts.append(
                MismatchDraft(PayoutMismatchKind.EXPECTED_NOT_PAID, employee_id, None, amount, None)
            )
    return drafts
