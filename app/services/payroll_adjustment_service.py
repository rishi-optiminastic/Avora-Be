"""Payroll adjustment business rules — HR/Admin manual tweaks, applied directly.

Only HR/Admin manage adjustments (no approval step, per the Settings tool); every
create/delete is audited (rule 5.7). The payroll estimate/register reads them via
the repository and applies them on top of the formula-derived slip. No FastAPI
objects here (Layering §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.payroll_adjustment import PayrollAdjustment
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.payroll_adjustment import PayrollAdjustmentRepository
from app.schemas.auth import CurrentUser
from app.schemas.payroll_adjustment import PayrollAdjustmentCreate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.can_manage_payroll


class PayrollAdjustmentService:
    def __init__(
        self,
        adjustments: PayrollAdjustmentRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._adjustments = adjustments
        self._employees = employees
        self._audit = audit

    async def list_for_period(
        self, caller: CurrentUser, period_month: str
    ) -> Sequence[PayrollAdjustment]:
        if not _can_manage(caller):
            raise AuthorizationError()
        return await self._adjustments.list_for_period(period_month)

    async def create(
        self, caller: CurrentUser, payload: PayrollAdjustmentCreate
    ) -> PayrollAdjustment:
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(payload.employee_id)
        if employee is None or not employee.is_active:
            raise ValidationError("Unknown or inactive employee.")
        row = await self._adjustments.create(payload, created_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.adjustment.create",
            target=f"adjustment:{row.id}:{payload.employee_id}:{payload.period_month}",
        )
        return row

    async def delete(self, caller: CurrentUser, adjustment_id: uuid.UUID) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        deleted = await self._adjustments.delete(adjustment_id)
        if not deleted:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="payroll.adjustment.delete",
            target=f"adjustment:{adjustment_id}",
        )
