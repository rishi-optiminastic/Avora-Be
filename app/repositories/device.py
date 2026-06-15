"""Device data access — enrollment, scoped reads, replay-safe sequence updates."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.employee import Employee, Role
from app.schemas.auth import CurrentUser


class DeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, device_id: uuid.UUID) -> Device | None:
        return await self._session.get(Device, device_id)

    def _scope_clause(self, caller: CurrentUser) -> ColumnElement[bool] | None:
        """Who may see a device? admin/IT/HR: all · senior_manager: dept ·
        manager: own + reports · everyone else: their own devices."""
        if caller.role in (Role.ADMIN, Role.IT_ADMIN, Role.HR):
            return None
        if caller.role is Role.SENIOR_MANAGER:
            caller_dept = (
                select(Employee.department)
                .where(Employee.id == caller.employee_id)
                .scalar_subquery()
            )
            owner_dept = (
                select(Employee.department)
                .where(Employee.id == Device.employee_id)
                .scalar_subquery()
            )
            return owner_dept == caller_dept
        if caller.role is Role.MANAGER:
            owner_manager = (
                select(Employee.manager_id)
                .where(Employee.id == Device.employee_id)
                .scalar_subquery()
            )
            return (Device.employee_id == caller.employee_id) | (
                owner_manager == caller.employee_id
            )
        return Device.employee_id == caller.employee_id

    async def create(self, *, employee_id: uuid.UUID, label: str, token_hash: str) -> Device:
        device = Device(employee_id=employee_id, label=label, token_hash=token_hash)
        self._session.add(device)
        await self._session.flush()
        return device

    async def list_for_scope(self, caller: CurrentUser) -> Sequence[Device]:
        stmt = select(Device)
        clause = self._scope_clause(caller)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = await self._session.execute(stmt.order_by(Device.created_at.desc()))
        return rows.scalars().all()

    async def revoke(self, device: Device) -> Device:
        device.is_revoked = True
        await self._session.flush()
        return device

    async def touch_last_seen(self, device: Device, seen_at: datetime) -> None:
        device.last_seen_at = seen_at
        await self._session.flush()

    async def get_active_by_token_hash(self, token_hash: str) -> Device | None:
        result = await self._session.execute(
            select(Device).where(
                Device.token_hash == token_hash,
                Device.is_revoked.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def advance_sequence(self, device: Device, sequence: int) -> None:
        """Move the high-water mark forward. Caller has already verified
        `sequence > device.last_sequence`."""
        device.last_sequence = sequence
        await self._session.flush()
