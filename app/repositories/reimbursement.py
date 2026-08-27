"""Reimbursement data access — the only place reimbursement queries are built.

Row-level scope mirrors leaves (Security rule 5.3): your own claims, your
reports'/department's, or everything (admin/HR), plus anything you reviewed as the
manager. Payroll reads approved claims org-wide via `approved_for_month`, which is
NOT scope-clamped — its only caller (PayrollService) authorizes first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, Role
from app.models.reimbursement import Reimbursement, ReimbursementStatus
from app.schemas.auth import CurrentUser
from app.schemas.reimbursement import ReimbursementCreate


class ReimbursementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        # HR and payroll-grant holders see every claim; Admin deliberately does
        # not (see CurrentUser.can_review_reimbursements) and falls through to the
        # own-claims clause below like any other employee.
        if caller.can_review_reimbursements:
            return None
        reviewed_by_me = Reimbursement.manager_reviewer_id == caller.employee_id
        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            requester_dept = (
                select(Employee.department)
                .where(Employee.id == Reimbursement.employee_id)
                .scalar_subquery()
            )
            return (requester_dept == caller_dept) | reviewed_by_me
        if caller.role is Role.MANAGER:
            requester_manager = (
                select(Employee.manager_id)
                .where(Employee.id == Reimbursement.employee_id)
                .scalar_subquery()
            )
            return (
                (Reimbursement.employee_id == caller.employee_id)
                | (requester_manager == caller.employee_id)
                | reviewed_by_me
            )
        return (Reimbursement.employee_id == caller.employee_id) | reviewed_by_me

    async def create(
        self, payload: ReimbursementCreate, *, employee_id: uuid.UUID, period_month: str
    ) -> Reimbursement:
        row = Reimbursement(
            employee_id=employee_id,
            amount_minor=payload.amount_minor,
            category=payload.category,
            description=payload.description,
            expense_date=payload.expense_date,
            period_month=period_month,
            status=ReimbursementStatus.SUBMITTED,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, reimbursement_id: uuid.UUID) -> Reimbursement | None:
        return await self._session.get(Reimbursement, reimbursement_id)

    async def get_in_scope(
        self, caller: CurrentUser, reimbursement_id: uuid.UUID
    ) -> Reimbursement | None:
        clause = self._scope_clause(caller)
        stmt = select(Reimbursement).where(Reimbursement.id == reimbursement_id)
        if clause is not None:
            stmt = stmt.where(clause)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_scope(
        self,
        caller: CurrentUser,
        *,
        offset: int,
        limit: int,
        status: ReimbursementStatus | None = None,
    ) -> tuple[Sequence[Reimbursement], int]:
        stmt = select(Reimbursement)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        if status is not None:
            stmt = stmt.where(Reimbursement.status == status)
        total = await self._session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
        rows = await self._session.execute(
            stmt.order_by(Reimbursement.expense_date.desc(), Reimbursement.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def approved_for_month(
        self, employee_ids: Sequence[uuid.UUID], period_month: str
    ) -> dict[uuid.UUID, int]:
        """Total fully-approved reimbursement minor-units per employee for a payroll
        month. Payroll (HR/Admin) is the only caller, so this is org-wide and not
        scope-clamped; the service authorizes before calling in."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(
                Reimbursement.employee_id,
                func.coalesce(func.sum(Reimbursement.amount_minor), 0),
            )
            .where(
                Reimbursement.employee_id.in_(employee_ids),
                Reimbursement.status == ReimbursementStatus.APPROVED,
                Reimbursement.period_month == period_month,
            )
            .group_by(Reimbursement.employee_id)
        )
        return {emp_id: int(total) for emp_id, total in rows.all()}

    async def flush(self) -> None:
        await self._session.flush()

    @staticmethod
    def period_for(expense_date: date) -> str:
        return f"{expense_date.year:04d}-{expense_date.month:02d}"
