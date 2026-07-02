"""Leave-allocation data access — the only place these queries are built.

No row-scope clause lives here: an allocation override is fetched one employee
at a time and the *service* authorizes the caller (HR/Admin) before calling in,
mirroring `CompensationRepository`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave_allocation import LeaveAllocation
from app.schemas.leave_allocation import LeaveAllocationWrite


class LeaveAllocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_employee(self, employee_id: uuid.UUID) -> LeaveAllocation | None:
        record: LeaveAllocation | None = await self._session.scalar(
            select(LeaveAllocation).where(LeaveAllocation.employee_id == employee_id)
        )
        return record

    async def upsert(
        self, employee_id: uuid.UUID, data: LeaveAllocationWrite, *, updated_by: uuid.UUID
    ) -> LeaveAllocation:
        record = await self.get_for_employee(employee_id)
        if record is None:
            record = LeaveAllocation(employee_id=employee_id)
            self._session.add(record)
        record.planned_days = data.planned_days
        record.sick_days = data.sick_days
        record.note = data.note
        record.updated_by = updated_by
        await self._session.flush()
        return record
