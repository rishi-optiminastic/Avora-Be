"""HR webhook business rules (Security rule 5.5).

The webhook may create/deactivate an employee and set org fields ONLY. It can
never set role/admin/privilege — that is enforced both by the input schema
(no such field exists) and by this service calling only `upsert_from_hr`, which
never writes `role`. Soft-delete on offboard; never hard-delete (§8).

People invited or added by hand before HR knew about them already have a row
under a synthetic external id. The first sync for their work email claims that
row rather than creating a second person; a work email already owned by a
different HR record is a conflict, never a silent merge.
"""

from __future__ import annotations

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.models.employee import Employee, EmployeeStatus, Role
from app.repositories.audit import AuditRepository
from app.repositories.employee import (
    SYNTHETIC_EXTERNAL_ID_PREFIXES,
    EmployeeRepository,
    has_synthetic_external_id,
)
from app.schemas.employee import HREmployeeUpsert

logger = get_logger("app.hr")


class HRService:
    def __init__(self, employees: EmployeeRepository, audit: AuditRepository) -> None:
        self._employees = employees
        self._audit = audit

    async def sync_employee(self, payload: HREmployeeUpsert) -> Employee:
        # Login maps the (lower-case) token email to `work_email` exactly, so
        # an address HR sends with capitals must never be stored as-is.
        email = str(payload.work_email).strip().lower()
        if payload.hr_external_id.startswith(SYNTHETIC_EXTERNAL_ID_PREFIXES):
            # Those prefixes mark rows created inside Avora; letting HR mint
            # them would make an HR record look claimable by a later sync.
            raise ValidationError("hr_external_id may not use a reserved prefix.")
        await self._link_to_existing_row(payload, email)

        # Only touch the reporting edge when HR actually sent one (see schema):
        # omitted = keep, explicit null = clear. An empty string is "unknown"
        # in some HR systems, never a deliberate clear.
        update_manager = (
            "manager_external_id" in payload.model_fields_set and payload.manager_external_id != ""
        )
        manager_id = None
        if payload.manager_external_id:
            manager = await self._employees.get_by_external_id(payload.manager_external_id)
            if manager is None:
                # HR named a manager we have not created yet - routine in a bulk
                # sync, where a report often arrives before their manager. An
                # unresolvable id is NOT the same as an explicit null, so keep
                # the current edge instead of clearing it. Clearing would be a
                # silent demotion: the reporting edge is what grants a lead
                # access to their team, so wiping it leaves their reports' leave
                # and regularizations invisible and un-actionable, with nothing
                # logged anywhere to explain it.
                logger.warning(
                    "hr_sync_manager_unresolved",
                    extra={"hr_external_id": payload.hr_external_id},
                )
                update_manager = False
            else:
                manager_id = manager.id

        employee = await self._employees.upsert_from_hr(
            hr_external_id=payload.hr_external_id,
            work_email=email,
            full_name=payload.full_name,
            department=payload.department,
            manager_id=manager_id,
            update_manager=update_manager,
            status=payload.status,
            biometric_id=payload.biometric_id,
            hire_date=payload.start_date.date() if payload.start_date else None,
            job_title=payload.job_title,
            location=payload.location,
            employee_number=payload.employee_number,
        )

        action = "hr.offboard" if payload.status is EmployeeStatus.INACTIVE else "hr.sync"
        await self._audit.append(
            actor="hr-webhook",
            action=action,
            target=f"employee:{employee.id}",
        )
        return employee

    async def _link_to_existing_row(self, payload: HREmployeeUpsert, email: str) -> None:
        """Make sure the upsert lands on the right row, or refuse.

        - Known HR id: fine, unless the new email belongs to someone else, or
          the email would change on a privileged account (below).
        - Unknown HR id, email on a PMS-created placeholder: claim it.
        - Unknown HR id, email on another HR record: conflict.
        """
        email_owner = await self._employees.find_by_work_email_insensitive(email)
        current = await self._employees.get_by_external_id(payload.hr_external_id)

        if current is not None:
            if email_owner is not None and email_owner.id != current.id:
                raise ConflictError("Work email already belongs to another employee.")
            if current.work_email.lower() != email:
                await self._guard_email_change(current, email)
            if payload.status is EmployeeStatus.INACTIVE and current.is_active:
                self._guard_deactivation(current)
            return
        if email_owner is None:
            return
        if not has_synthetic_external_id(email_owner):
            raise ConflictError("Work email already belongs to another HR record.")

        await self._employees.claim_for_hr(email_owner, payload.hr_external_id)
        await self._audit.append(
            actor="hr-webhook",
            action="hr.claim",
            target=f"employee:{email_owner.id}",
        )

    async def _guard_email_change(self, employee: Employee, new_email: str) -> None:
        """Login is keyed on email, so changing it hands the account - and its
        role - to whoever owns the new address. HR may do that for an ordinary
        employee; on a privileged account it must be done inside Avora by an
        admin (rule 5.5: the webhook never moves privilege)."""
        if employee.role is not Role.EMPLOYEE or employee.payroll_manager:
            raise ConflictError(
                "Email change on a privileged account must be made in Avora by an admin."
            )
        await self._audit.append(
            actor="hr-webhook",
            action="hr.email_change",
            target=f"employee:{employee.id}",
        )

    @staticmethod
    def _guard_deactivation(employee: Employee) -> None:
        """A wrong status in the HR system must never lock out the people who
        run Avora: privileged accounts are deactivated by an admin in Avora."""
        if employee.role is not Role.EMPLOYEE or employee.payroll_manager:
            raise ConflictError(
                "Deactivating a privileged account must be done in Avora by an admin."
            )
