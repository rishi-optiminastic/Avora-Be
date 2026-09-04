"""Reimbursement business rules — a two-step expense-claim approval.

An employee submits their own claim. Their reporting MANAGER approves or rejects
it first (never their own; HR/Admin may also act at this step when there is no
manager). Only a manager-approved claim reaches HR/Admin for FINAL approval.
Approved claims are read by payroll (org-wide) via the repository.

Reads are scoped in the repository. No FastAPI objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.uploads import detect_media_type
from app.models.employee import Role
from app.models.notification import NotificationKind, NotificationLevel
from app.models.reimbursement import (
    Reimbursement,
    ReimbursementReceipt,
    ReimbursementStatus,
)
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.payslip import PayslipRepository
from app.repositories.reimbursement import ReimbursementRepository
from app.schemas.auth import CurrentUser
from app.schemas.reimbursement import ReimbursementCreate, ReimbursementDecision
from app.services.notification_service import NotificationService

logger = get_logger("app.reimbursement")

_LINK = "/dashboard/time/reimbursements"

# An invoice is a receipt photo or a PDF, not a data dump — a tight cap keeps
# the in-DB fallback (used when S3 is off) from bloating the row store.
MAX_RECEIPT_BYTES = 10 * 1024 * 1024
# A claim can carry several documents (invoice + toll slip + boarding pass), but
# not an unbounded pile — reviewers have to read them, and each one is stored.
MAX_RECEIPTS_PER_CLAIM = 10
MAX_RECEIPT_LABEL = 120


def _receipt_label(label: str | None, filename: str | None) -> str:
    """What to call this proof. The claimant's own name wins; failing that the
    filename without its extension; failing that a plain word, because an unnamed
    row in a reviewer's list is worse than a generic one."""
    named = (label or "").strip()
    if named:
        return named[:MAX_RECEIPT_LABEL]
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].strip()
    return (stem or "Proof")[:MAX_RECEIPT_LABEL]
# What a proof may be. Enforced by sniffing the file's own bytes, never by the
# Content-Type the browser claims — see app/core/uploads.detect_media_type.
ALLOWED_RECEIPT_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/webp"}
)


def _can_review_hr(caller: CurrentUser) -> bool:
    """The final-approval step. HR and payroll-grant holders only — Admin is
    deliberately excluded (see CurrentUser.can_review_reimbursements)."""
    return caller.can_review_reimbursements


def _money(amount_minor: int) -> str:
    return f"₹{amount_minor / 100:,.2f}"


class ReimbursementService:
    def __init__(
        self,
        reimbursements: ReimbursementRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        settings: Settings,
        payslips: PayslipRepository,
    ) -> None:
        self._reimbursements = reimbursements
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._settings = settings
        self._payslips = payslips

    async def add_receipt(
        self,
        caller: CurrentUser,
        reimbursement_id: uuid.UUID,
        data: bytes,
        *,
        label: str | None,
        filename: str | None,
        content_type: str,  # the browser's claim; kept for logs, not trusted
    ) -> Reimbursement:
        """Attach one named proof to a claim. The CLAIMANT's own, and only while the
        claim can still change — once it is finally approved the attachments are part
        of a paid record and must not be added to or swapped.

        Bytes go to S3 when configured (only the key is stored), else to the in-DB
        `content` column — the same fallback documents and screenshots use.
        """
        row = await self._editable_claim(caller, reimbursement_id)
        if len(row.receipts) >= MAX_RECEIPTS_PER_CLAIM:
            raise ValidationError(
                f"A claim can carry at most {MAX_RECEIPTS_PER_CLAIM} proofs. "
                "Remove one before adding another."
            )
        if not data:
            raise ValidationError("The file is empty.")
        if len(data) > MAX_RECEIPT_BYTES:
            raise ValidationError("That file is too large (max 10 MB).")

        # The browser's Content-Type is a claim, and a wrong one often enough that
        # trusting it rejected real PDFs (many systems send them as
        # application/octet-stream). Decide from the bytes instead — which also
        # means a script cannot get in by calling itself a PDF.
        media_type = detect_media_type(data)
        if media_type is None or media_type not in ALLOWED_RECEIPT_TYPES:
            raise ValidationError("Proofs must be a PDF, PNG, JPG or WebP.")

        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.workspace_object_key(uuid.uuid4().hex, filename)
            try:
                await storage.put_object(object_key, data, media_type)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("reimbursement_receipt_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        await self._reimbursements.add_receipt(
            row,
            label=_receipt_label(label, filename),
            object_key=object_key,
            content=stored,
            content_type=media_type,
            filename=filename,
            size_bytes=len(data),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.receipt_attach",
            target=f"reimbursement:{row.id}",
        )
        return row

    async def remove_receipt(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, receipt_id: uuid.UUID
    ) -> Reimbursement:
        """Drop one proof from a claim the claimant can still edit."""
        row = await self._editable_claim(caller, reimbursement_id)
        receipt = await self._reimbursements.get_receipt(row, receipt_id)
        if receipt is None:
            raise NotFoundError()
        await self._reimbursements.delete_receipt(row, receipt)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.receipt_remove",
            target=f"reimbursement:{row.id}",
        )
        return row

    async def _editable_claim(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement:
        """The caller's OWN claim, while it is still open to change. Shared by every
        attachment write so the ownership and status gates can never drift apart."""
        row = await self._reimbursements.get_in_scope(caller, reimbursement_id)
        if row is None:
            raise NotFoundError()
        if row.employee_id != caller.employee_id:
            raise AuthorizationError()
        if row.status not in (
            ReimbursementStatus.SUBMITTED,
            ReimbursementStatus.MANAGER_APPROVED,
        ):
            raise ValidationError("This claim can no longer be edited.")
        return row

    async def get_receipt(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, receipt_id: uuid.UUID
    ) -> ReimbursementReceipt:
        """The proof whose bytes the download endpoint will stream.

        Scope is the claim's own (own / reports' / HR + payroll) — a reviewer must
        be able to see what they are approving. 404 when out of scope, when the
        claim has no such proof, or when the id belongs to someone else's claim, so
        neither existence nor absence leaks.
        """
        row = await self._reimbursements.get_in_scope(caller, reimbursement_id)
        if row is None:
            raise NotFoundError()
        receipt = await self._reimbursements.get_receipt(row, receipt_id)
        if receipt is None:
            raise NotFoundError()
        return receipt

    async def list_for_caller(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: ReimbursementStatus | None = None,
    ) -> tuple[Sequence[Reimbursement], int]:
        return await self._reimbursements.list_for_scope(
            caller, offset=offset, limit=limit, status=status
        )

    async def get_for_caller(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement:
        row = await self._reimbursements.get_in_scope(caller, reimbursement_id)
        if row is None:
            raise NotFoundError()
        return row

    async def submit(self, caller: CurrentUser, payload: ReimbursementCreate) -> Reimbursement:
        period = ReimbursementRepository.period_for(payload.expense_date)
        row = await self._reimbursements.create(
            payload, employee_id=caller.employee_id, period_month=period
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.submit",
            target=f"reimbursement:{row.id}",
        )
        await self._notify_step1_reviewers(caller, row)
        return row

    async def _notify_step1_reviewers(
        self, caller: CurrentUser, row: Reimbursement
    ) -> None:
        """Ping whoever performs the manager step: the reporting manager, or every
        HR/Admin when the employee has no manager on file."""
        applicant = await self._employees.get(caller.employee_id)
        who = applicant.full_name if applicant is not None else "An employee"
        body = f"{who} · {_money(row.amount_minor)} · {row.category.value}"
        recipients: list[uuid.UUID] = []
        if caller.manager_id is not None:
            recipients.append(caller.manager_id)
        else:
            recipients = [
                r.id
                for r in (
                    *await self._employees.list_by_role(Role.HR),
                    *await self._employees.list_payroll_managers(),
                )
            ]
        seen: set[uuid.UUID] = set()
        for recipient_id in recipients:
            if recipient_id in seen or recipient_id == caller.employee_id:
                continue
            seen.add(recipient_id)
            await self._notifications.notify(
                recipient_id=recipient_id,
                kind=NotificationKind.REIMBURSEMENT_SUBMITTED,
                title="Reimbursement to review",
                body=body,
                level=NotificationLevel.INFO,
                link=_LINK,
                entity_type="reimbursement",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )

    async def manager_decide(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, payload: ReimbursementDecision
    ) -> Reimbursement:
        """Step 1: the applicant's reporting manager (or HR/Admin) approves/rejects."""
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if caller.employee_id == row.employee_id:
            raise AuthorizationError()  # never your own claim
        if not await self._is_manager_reviewer(caller, row):
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.SUBMITTED:
            raise ConflictError("This claim is no longer awaiting manager review.")

        row.manager_reviewer_id = caller.employee_id
        row.manager_decided_at = datetime.now(UTC)
        row.manager_note = payload.note
        row.status = (
            ReimbursementStatus.MANAGER_APPROVED
            if payload.approve
            else ReimbursementStatus.REJECTED
        )
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.manager_decide",
            target=f"reimbursement:{row.id}:{row.status.value}",
        )
        if payload.approve:
            await self._notify_hr_reviewers(caller, row)
        else:
            await self._notify_applicant(row, approved=False, note=payload.note)
        return row

    async def hr_decide(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, payload: ReimbursementDecision
    ) -> Reimbursement:
        """Step 2: HR/Admin final approval of a manager-approved claim."""
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if not _can_review_hr(caller) or caller.employee_id == row.employee_id:
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.MANAGER_APPROVED:
            raise ConflictError("This claim is not awaiting HR approval.")

        if payload.approve:
            row.period_month = await self._settlement_month(row, payload.settlement_month)
        row.hr_reviewer_id = caller.employee_id
        row.hr_decided_at = datetime.now(UTC)
        row.hr_note = payload.note
        row.status = (
            ReimbursementStatus.APPROVED if payload.approve else ReimbursementStatus.REJECTED
        )
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.hr_decide",
            target=f"reimbursement:{row.id}:{row.status.value}",
        )
        await self._notify_applicant(row, approved=payload.approve, note=payload.note)
        return row

    async def _settlement_month(self, row: Reimbursement, chosen: str | None) -> str:
        """Which payroll month pays this claim out.

        HR's choice wins; otherwise the claim keeps the month it was filed against
        (the expense month). Either way the month must still be OPEN: paying into
        a month whose payslips are already released means the money is never
        actually handed over, because those slips are frozen snapshots. Refusing
        loudly here beats an approved claim that quietly never gets paid.
        """
        month = chosen or row.period_month
        if await self._payslips.list_for_month(month):
            raise ConflictError(
                f"Payroll for {month} has already been released, so nothing more can be "
                "paid into it. Settle this claim in a later month."
            )
        return month

    async def move_settlement_month(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, settlement_month: str
    ) -> Reimbursement:
        """Move an APPROVED claim into a different payroll month.

        The month is only decided once, at approval, and until now it was frozen
        afterwards — so a claim approved into the wrong run sat there with no way
        out. HR/finance can move it, subject to both ends being open:

        - the month it is LEAVING must not be released, or it has already been
          paid and moving it would quietly un-pay someone;
        - the month it is GOING TO must not be released, or the money lands in a
          frozen snapshot and is never handed over.
        """
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if not _can_review_hr(caller):
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.APPROVED:
            raise ConflictError("Only an approved claim is scheduled into a payroll month.")
        if settlement_month == row.period_month:
            return row
        if await self._payslips.list_for_month(row.period_month):
            raise ConflictError(
                f"This claim was already paid in {row.period_month}'s released payroll, "
                "so it cannot be moved out of it."
            )
        previous = row.period_month
        row.period_month = await self._settlement_month(row, settlement_month)
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.settlement_month_move",
            target=f"reimbursement:{row.id}:{previous}->{row.period_month}",
        )
        return row

    async def revoke_approval(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID, note: str | None
    ) -> Reimbursement:
        """Take an approved claim back out of payroll altogether.

        For a claim that should not be paid at all, rather than one filed against
        the wrong month. It becomes REJECTED — the history stays, the claim simply
        stops being payable. Refused once the money has actually gone out.
        """
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if not _can_review_hr(caller):
            raise AuthorizationError()
        if row.status is not ReimbursementStatus.APPROVED:
            raise ConflictError("This claim is not approved.")
        if await self._payslips.list_for_month(row.period_month):
            raise ConflictError(
                f"This claim was already paid in {row.period_month}'s released payroll. "
                "Recover it through a payroll adjustment instead."
            )
        row.status = ReimbursementStatus.REJECTED
        row.hr_reviewer_id = caller.employee_id
        row.hr_decided_at = datetime.now(UTC)
        row.hr_note = note
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.approval_revoke",
            target=f"reimbursement:{row.id}",
        )
        await self._notify_applicant(row, approved=False, note=note)
        return row

    async def withdraw(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement:
        row = await self._reimbursements.get(reimbursement_id)
        if row is None:
            raise NotFoundError()
        if row.employee_id != caller.employee_id:
            raise AuthorizationError()
        if row.status not in (
            ReimbursementStatus.SUBMITTED,
            ReimbursementStatus.MANAGER_APPROVED,
        ):
            raise ConflictError("Only a claim still under review can be withdrawn.")
        row.status = ReimbursementStatus.WITHDRAWN
        await self._reimbursements.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reimbursement.withdraw",
            target=f"reimbursement:{row.id}",
        )
        return row

    async def _is_manager_reviewer(
        self, caller: CurrentUser, row: Reimbursement
    ) -> bool:
        """The manager step is for the applicant's reporting manager. HR/Admin may
        also act here (e.g. when the applicant has no manager on file)."""
        if _can_review_hr(caller):
            return True
        applicant = await self._employees.get(row.employee_id)
        return applicant is not None and applicant.manager_id == caller.employee_id

    async def _notify_hr_reviewers(self, caller: CurrentUser, row: Reimbursement) -> None:
        applicant = await self._employees.get(row.employee_id)
        who = applicant.full_name if applicant is not None else "An employee"
        body = f"{who} · {_money(row.amount_minor)} · manager-approved"
        seen: set[uuid.UUID] = set()
        for reviewer in (
            *await self._employees.list_by_role(Role.HR),
            *await self._employees.list_payroll_managers(),
        ):
            if reviewer.id in seen or reviewer.id == row.employee_id:
                continue
            seen.add(reviewer.id)
            await self._notifications.notify(
                recipient_id=reviewer.id,
                kind=NotificationKind.REIMBURSEMENT_SUBMITTED,
                title="Reimbursement for final approval",
                body=body,
                level=NotificationLevel.INFO,
                link=_LINK,
                entity_type="reimbursement",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )

    async def _notify_applicant(
        self, row: Reimbursement, *, approved: bool, note: str | None
    ) -> None:
        await self._notifications.notify(
            recipient_id=row.employee_id,
            kind=NotificationKind.REIMBURSEMENT_DECISION,
            title=f"Reimbursement {'approved' if approved else 'declined'}",
            body=note or f"{_money(row.amount_minor)} · {row.category.value}",
            level=NotificationLevel.INFO,
            link=_LINK,
            entity_type="reimbursement",
            entity_id=row.id,
            actor_id=row.hr_reviewer_id or row.manager_reviewer_id,
        )
