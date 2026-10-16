"""Payout verification - check the bank payout file against released payroll.

Accounts uploads the file they paid a month from. Every line is matched to an
employee (employee number, then bank account, then work email), compared with
what Avora says that run should have paid, and each disagreement is stored for
HR to resolve with a note. When anything disagrees, HR, admins and payroll
managers are flagged in-app and by email - once per upload, best-effort, so a
failed send never undoes the verification. The verification is committed
BEFORE anyone is told, and the in-app flags (with `notified_at`) are committed
before any email goes out, so nobody is ever mailed about an upload that then
rolls back. The same file uploaded twice for a month and run is refused (409)
instead of being stored - and alerted - again.

A line tied to a person by employee number or email but paying a different
bank account than the one on file is its own mismatch (ACCOUNT_MISMATCH).
Only the last four digits of any account are stored, returned or logged.

What "should have paid" means follows the export the file was paid from
(`PayoutRunKind`): the released payslip's net pay (reimbursements folded in),
the same net without reimbursements, or the approved reimbursements alone - each
in the whole rupees the register carries (`register_minor`). The reimbursements
run expects claims for active employees only, the roster
`PayrollService.export_reimbursements_xlsx` builds its file from (via
`estimate()` / `list_all_active`): a leaver's approved claim never reaches that
export, so expecting it would flag EXPECTED_NOT_PAID on every upload forever.

Payroll managers only (HR/Admin or the payroll-manager grant); payroll is
org-wide for them, as everywhere else in the payroll cluster. Uploads, views and
resolutions are audited (rule 5.7). No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import asyncio
import calendar
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256

from cryptography.fernet import InvalidToken

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.payout_file import (
    ParsedPayoutFile,
    ParsedPayoutRow,
    PayoutFileError,
    normalize_account_number,
    parse_payout_file,
)
from app.core.pii_crypto import PiiCryptoError, decrypt_pii
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.payout_verification import (
    PayoutMismatch,
    PayoutMismatchKind,
    PayoutRow,
    PayoutRowStatus,
    PayoutRunKind,
    PayoutUpload,
)
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.payout_verification import PayoutVerificationRepository
from app.repositories.payroll_settings import PayrollSettingsRepository
from app.repositories.payslip import PayslipRepository
from app.repositories.reimbursement import ReimbursementRepository
from app.schemas.auth import CurrentUser
from app.schemas.payout_verification import (
    PayoutMismatchCount,
    PayoutMismatchRead,
    PayoutUploadRead,
    PayoutVerificationRead,
)
from app.services.email_service import EmailService
from app.services.notification_service import NotificationService
from app.services.payout_emails import payout_mismatch_email
from app.services.payout_reconcile import (
    EmployeeIndex,
    Identity,
    MismatchDraft,
    PaidLine,
    reconcile,
    register_minor,
)

logger = get_logger("app.payout_verification")

# Settings > Payroll > Payout Check (the settings page reads `tab`, the payroll
# panel reads `view`).
REVIEW_PATH = "/dashboard/admin/settings?tab=payroll&view=payout"
_ACCOUNT_TAIL = 4
_MAX_FILENAME = 255


def _month_label(month: str) -> str:
    year, number = month.split("-")
    return f"{calendar.month_name[int(number)]} {year}"


def _last4(account: str | None) -> str | None:
    return account[-_ACCOUNT_TAIL:] if account else None


def _row_status(row: ParsedPayoutRow, employee_id: uuid.UUID | None) -> PayoutRowStatus:
    if row.failed:
        return PayoutRowStatus.FAILED
    return PayoutRowStatus.MATCHED if employee_id is not None else PayoutRowStatus.UNMATCHED


class PayoutVerificationService:
    def __init__(
        self,
        payouts: PayoutVerificationRepository,
        payslips: PayslipRepository,
        reimbursements: ReimbursementRepository,
        compensation: CompensationRepository,
        employees: EmployeeRepository,
        payroll_settings: PayrollSettingsRepository,
        notifications: NotificationService,
        email: EmailService,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._payouts = payouts
        self._payslips = payslips
        self._reimbursements = reimbursements
        self._compensation = compensation
        self._employees = employees
        self._payroll_settings = payroll_settings
        self._notifications = notifications
        self._email = email
        self._audit = audit
        self._settings = settings

    # ---- public API -------------------------------------------------------- #
    async def upload(
        self,
        caller: CurrentUser,
        *,
        month: str,
        kind: PayoutRunKind,
        data: bytes,
        filename: str | None,
    ) -> PayoutVerificationRead:
        """Store an uploaded payout file, verify it, and flag mismatches to HR."""
        self._authorize(caller)
        digest = sha256(data).hexdigest()
        await self._refuse_repeat(month, kind, digest)
        try:
            # openpyxl is synchronous and CPU-bound: keep it off the event loop.
            parsed = await asyncio.to_thread(parse_payout_file, data)
        except PayoutFileError as exc:
            raise ValidationError(str(exc)) from exc
        expected, currency = await self._expected(month, kind)
        upload = await self._store_upload(
            caller, month, kind, parsed, digest=digest, filename=filename, currency=currency
        )
        index = EmployeeIndex.build(await self._identities())
        rows, lines = await self._store_rows(upload, parsed.rows, index)
        drafts = reconcile(expected, lines)
        await self._payouts.add_mismatches([self._mismatch(upload.id, d, rows) for d in drafts])
        self._fill_totals(upload, rows, expected, len(drafts))
        await self._payouts.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.payout.upload",
            target=f"payout_upload:{upload.id}:month:{month}:rows:{len(rows)}",
        )
        await self._payouts.commit()  # durable before HR hears about it
        await self.flag_to_hr(upload, actor_id=caller.employee_id)
        view = await self._verification(upload)
        return view.model_copy(update={"warnings": list(parsed.warnings)})

    async def list_uploads(
        self, caller: CurrentUser, month: str | None, *, limit: int, offset: int
    ) -> tuple[list[PayoutUploadRead], int]:
        self._authorize(caller)
        rows, total = await self._payouts.list_uploads(month, limit=limit, offset=offset)
        return [PayoutUploadRead.model_validate(r) for r in rows], total

    async def latest(self, caller: CurrentUser, month: str | None) -> PayoutVerificationRead:
        self._authorize(caller)
        upload = await self._payouts.latest_upload(month)
        if upload is None:
            raise NotFoundError()
        return await self._viewed(caller, upload)

    async def get(self, caller: CurrentUser, upload_id: uuid.UUID) -> PayoutVerificationRead:
        self._authorize(caller)
        upload = await self._payouts.get_upload(upload_id)
        if upload is None:
            raise NotFoundError()
        return await self._viewed(caller, upload)

    async def resolve(
        self, caller: CurrentUser, upload_id: uuid.UUID, mismatch_id: uuid.UUID, note: str
    ) -> PayoutMismatchRead:
        """Close one mismatch with a note saying what was done about it. Audited."""
        self._authorize(caller)
        mismatch = await self._payouts.get_mismatch(upload_id, mismatch_id)
        if mismatch is None:
            raise NotFoundError()
        if mismatch.resolved_at is not None:
            raise ConflictError("This mismatch is already resolved.")
        mismatch.resolved_at = datetime.now(UTC)
        mismatch.resolved_by = caller.employee_id
        mismatch.resolution_note = note
        await self._payouts.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.payout.resolve",
            target=f"payout_mismatch:{mismatch.id}:upload:{upload_id}",
        )
        (read,) = await self._mismatch_reads([mismatch])
        return read

    async def flag_to_hr(self, upload: PayoutUpload, *, actor_id: uuid.UUID | None) -> bool:
        """Tell HR, admins and payroll managers that a payout disagrees with payroll.

        At most once per upload (`notified_at`), and only when something is off.
        The claim and the in-app flags are committed first, then the emails go
        out, so a mail never describes state that could still roll back.
        Best-effort: the in-app notification isolates its own failure and an email
        failure is logged and skipped, so the verification is never rolled back.
        """
        if upload.mismatch_count == 0 or upload.notified_at is not None:
            return False
        upload.notified_at = datetime.now(UTC)
        await self._payouts.flush()
        counts = Counter(m.kind.value for m in await self._payouts.mismatches_for(upload.id))
        label = _month_label(upload.period_month)
        noun = "mismatch" if upload.mismatch_count == 1 else "mismatches"
        recipients = await self._recipients(exclude=actor_id)
        for recipient_id, _email in recipients:
            await self._notifications.notify(
                recipient_id=recipient_id,
                kind=NotificationKind.SYSTEM,
                title=f"Payout check: {upload.mismatch_count} {noun} in {label}",
                body="The bank payout file does not match released payroll.",
                level=NotificationLevel.WARNING,
                link=REVIEW_PATH,
                entity_type="payout_upload",
                entity_id=upload.id,
                actor_id=actor_id,
            )
        await self._payouts.commit()
        subject, html = payout_mismatch_email(
            month_label=label, counts=dict(counts), review_url=self._url(REVIEW_PATH)
        )
        for _recipient_id, email in recipients:
            await self._send_quietly(email, subject, html, upload.id)
        return True

    # ---- verification ------------------------------------------------------ #
    async def _expected(self, month: str, kind: PayoutRunKind) -> tuple[dict[uuid.UUID, int], str]:
        """Minor units each employee should receive from this run, and its currency."""
        configured = await self._payroll_settings.get()
        currency = configured.currency if configured is not None else "INR"
        if kind is PayoutRunKind.REIMBURSEMENTS:
            # The reimbursement export's roster: active employees only (module doc).
            ids = [e.id for e in await self._employees.list_all_active()]
            approved = await self._reimbursements.approved_for_month(ids, month)
            amounts = {emp: register_minor(v) for emp, v in approved.items()}
            return {emp: v for emp, v in amounts.items() if v > 0}, currency
        payslips = await self._payslips.list_for_month(month)
        if not payslips:
            raise ValidationError(
                f"{_month_label(month)} payroll has not been released yet. "
                "Finalize the month before checking its payout."
            )
        expected: dict[uuid.UUID, int] = {}
        for slip in payslips:
            net = slip.net_minor
            if kind is PayoutRunKind.SALARY_ONLY:
                net -= slip.reimbursement_minor
            if register_minor(net) > 0:
                expected[slip.employee_id] = register_minor(net)
        return expected, payslips[0].currency or currency

    async def _identities(self) -> list[Identity]:
        people = await self._payouts.employee_identities()
        comps = await self._compensation.get_for_employees([p.id for p in people])
        identities: list[Identity] = []
        for person in people:
            comp = comps.get(person.id)
            account = self._account(comp.account_number_encrypted if comp else None)
            identities.append(
                Identity(person.id, person.employee_number, person.work_email, account)
            )
        return identities

    def _account(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return decrypt_pii(self._settings, ciphertext)
        except (InvalidToken, PiiCryptoError, ValueError):
            logger.warning("payout_account_decrypt_failed")
            return None

    async def _refuse_repeat(self, month: str, kind: PayoutRunKind, digest: str) -> None:
        """409 for a file already checked for this month and run: storing it again
        would only duplicate the mismatches and alert HR a second time."""
        earlier = await self._payouts.find_same_file(month, kind, digest)
        if earlier is None:
            return
        raise ConflictError(
            f"This exact file was already checked for {_month_label(month)} on "
            f"{earlier.created_at:%d %b %Y}. Open that check instead of uploading it again."
        )

    async def _store_upload(
        self,
        caller: CurrentUser,
        month: str,
        kind: PayoutRunKind,
        parsed: ParsedPayoutFile,
        *,
        digest: str,
        filename: str | None,
        currency: str,
    ) -> PayoutUpload:
        return await self._payouts.add_upload(
            PayoutUpload(
                period_month=month,
                kind=kind,
                filename=(filename or "payout")[:_MAX_FILENAME],
                file_type=parsed.file_type.value,
                file_sha256=digest,
                identifier_columns=",".join(f.value for f in parsed.fields),
                currency=currency,
                uploaded_by=caller.employee_id,
                row_count=len(parsed.rows),
            )
        )

    async def _store_rows(
        self, upload: PayoutUpload, parsed_rows: Sequence[ParsedPayoutRow], index: EmployeeIndex
    ) -> tuple[list[PayoutRow], list[PaidLine]]:
        """Persist each line (account as last four only) and its reconcile view."""
        rows: list[PayoutRow] = []
        lines: list[PaidLine] = []
        for position, parsed in enumerate(parsed_rows):
            employee_id, matched_by = index.match(parsed)
            rows.append(
                PayoutRow(
                    upload_id=upload.id,
                    row_number=parsed.row_number,
                    employee_id=employee_id,
                    matched_by=matched_by,
                    employee_number=parsed.employee_number,
                    account_last4=_last4(parsed.account_number),
                    email=parsed.email,
                    payee_name=parsed.payee_name,
                    amount_minor=parsed.amount_minor,
                    reference=parsed.reference,
                    bank_status=parsed.bank_status,
                    status=_row_status(parsed, employee_id),
                )
            )
            lines.append(
                PaidLine(
                    position,
                    employee_id,
                    parsed.amount_minor,
                    parsed.failed,
                    account_differs=index.account_differs(parsed, employee_id),
                )
            )
        await self._payouts.add_rows(rows)
        return rows, lines

    @staticmethod
    def _mismatch(
        upload_id: uuid.UUID, draft: MismatchDraft, rows: list[PayoutRow]
    ) -> PayoutMismatch:
        return PayoutMismatch(
            upload_id=upload_id,
            kind=draft.kind,
            employee_id=draft.employee_id,
            row_id=rows[draft.row_index].id if draft.row_index is not None else None,
            expected_minor=draft.expected_minor,
            paid_minor=draft.paid_minor,
        )

    @staticmethod
    def _fill_totals(
        upload: PayoutUpload, rows: list[PayoutRow], expected: dict[uuid.UUID, int], mismatches: int
    ) -> None:
        succeeded = [r for r in rows if r.status is not PayoutRowStatus.FAILED]
        upload.matched_count = sum(1 for r in rows if r.status is PayoutRowStatus.MATCHED)
        upload.failed_count = len(rows) - len(succeeded)
        # Zero / negative Net Pay rows moved no money (see PaidLine.pays).
        upload.paid_total_minor = sum(r.amount_minor for r in succeeded if r.amount_minor > 0)
        upload.expected_count = len(expected)
        upload.expected_total_minor = sum(expected.values())
        upload.mismatch_count = mismatches

    # ---- reads ------------------------------------------------------------- #
    async def _viewed(self, caller: CurrentUser, upload: PayoutUpload) -> PayoutVerificationRead:
        view = await self._verification(upload)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.payout.view",
            target=f"payout_upload:{upload.id}",
        )
        return view

    async def _verification(self, upload: PayoutUpload) -> PayoutVerificationRead:
        mismatches = list(await self._payouts.mismatches_for(upload.id))
        totals = Counter(m.kind for m in mismatches)
        open_ = Counter(m.kind for m in mismatches if m.resolved_at is None)
        counts = [
            PayoutMismatchCount(kind=k, total=totals.get(k, 0), open=open_.get(k, 0))
            for k in PayoutMismatchKind
        ]
        return PayoutVerificationRead(
            upload=PayoutUploadRead.model_validate(upload),
            counts=counts,
            mismatches=await self._mismatch_reads(mismatches),
        )

    async def _mismatch_reads(
        self, mismatches: Sequence[PayoutMismatch]
    ) -> list[PayoutMismatchRead]:
        rows = await self._payouts.rows_by_id([m.row_id for m in mismatches if m.row_id])
        people_ids = {m.employee_id for m in mismatches if m.employee_id}
        people_ids |= {m.resolved_by for m in mismatches if m.resolved_by}
        people = await self._employees.get_many(list(people_ids))
        on_file = await self._on_file_last4(mismatches)
        reads: list[PayoutMismatchRead] = []
        for m in mismatches:
            row = rows.get(m.row_id) if m.row_id else None
            person = people.get(m.employee_id) if m.employee_id else None
            resolver = people.get(m.resolved_by) if m.resolved_by else None
            reads.append(
                PayoutMismatchRead(
                    id=m.id,
                    kind=m.kind,
                    employee_id=m.employee_id,
                    employee_name=person.full_name if person else None,
                    employee_number=person.employee_number if person else None,
                    payee_name=row.payee_name if row else None,
                    row_number=row.row_number if row else None,
                    reference=row.reference if row else None,
                    account_last4=row.account_last4 if row else None,
                    account_on_file_last4=on_file.get(m.employee_id) if m.employee_id else None,
                    expected_minor=m.expected_minor,
                    paid_minor=m.paid_minor,
                    difference_minor=(m.paid_minor or 0) - (m.expected_minor or 0),
                    resolved_at=m.resolved_at,
                    resolved_by=m.resolved_by,
                    resolved_by_name=resolver.full_name if resolver else None,
                    resolution_note=m.resolution_note,
                )
            )
        return reads

    async def _on_file_last4(self, mismatches: Sequence[PayoutMismatch]) -> dict[uuid.UUID, str]:
        """Last four of the account on file, for account mismatches only."""
        ids = [
            m.employee_id
            for m in mismatches
            if m.kind is PayoutMismatchKind.ACCOUNT_MISMATCH and m.employee_id
        ]
        if not ids:
            return {}
        comps = await self._compensation.get_for_employees(ids)
        tails: dict[uuid.UUID, str] = {}
        for employee_id, comp in comps.items():
            tail = _last4(normalize_account_number(self._account(comp.account_number_encrypted)))
            if tail:
                tails[employee_id] = tail
        return tails

    # ---- helpers ----------------------------------------------------------- #
    @staticmethod
    def _authorize(caller: CurrentUser) -> None:
        if not caller.can_manage_payroll:
            raise AuthorizationError()

    async def _recipients(self, *, exclude: uuid.UUID | None) -> list[tuple[uuid.UUID, str]]:
        """HR, admins and payroll managers - each once, never the uploader."""
        seen: set[uuid.UUID] = set()
        people: list[tuple[uuid.UUID, str]] = []
        for person in (
            *await self._employees.list_by_role(Role.HR),
            *await self._employees.list_admins(),
            *await self._employees.list_payroll_managers(),
        ):
            if person.id in seen or person.id == exclude:
                continue
            seen.add(person.id)
            people.append((person.id, person.work_email))
        return people

    async def _send_quietly(self, to: str, subject: str, html: str, upload_id: uuid.UUID) -> None:
        if not to:
            return
        try:
            await self._email.send(to=to, subject=subject, html=html)
        except Exception:  # best-effort: any provider failure must not undo the check
            logger.warning("payout_mismatch_email_failed", extra={"upload": str(upload_id)})

    def _url(self, path: str) -> str:
        return f"{self._settings.app_base_url.rstrip('/')}{path}"
