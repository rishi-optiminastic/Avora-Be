"""HR webhook business rules (Security rule 5.5).

The webhook may create/deactivate an employee and set org fields ONLY. It can
never set role/admin/privilege — that is enforced both by the input schema
(no such field exists) and by this service calling only `upsert_from_hr`, which
never writes `role`. Soft-delete on offboard; never hard-delete (§8).
"""

from __future__ import annotations

from app.models.employee import Employee, EmployeeStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.employee import HREmployeeUpsert


class HRService:
    def __init__(self, employees: EmployeeRepository, audit: AuditRepository) -> None:
        self._employees = employees
        self._audit = audit

    async def sync_employee(self, payload: HREmployeeUpsert) -> Employee:
        # Resolve the reporting manager (if any) by HR external id.
        manager_id = None
        if payload.manager_external_id:
            manager = await self._employees.get_by_external_id(payload.manager_external_id)
            manager_id = manager.id if manager else None

        employee = await self._employees.upsert_from_hr(
            hr_external_id=payload.hr_external_id,
            work_email=str(payload.work_email),
            full_name=payload.full_name,
            department=payload.department,
            manager_id=manager_id,
            status=payload.status,
        )

        action = "hr.offboard" if payload.status is EmployeeStatus.INACTIVE else "hr.sync"
        await self._audit.append(
            actor="hr-webhook",
            action=action,
            target=f"employee:{employee.id}",
        )
        return employee
