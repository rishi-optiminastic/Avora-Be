"""Payout verification data access - the only place these queries are built.

Payout verification is an org-wide payroll concern: the service authorizes the
caller as a payroll manager (HR/Admin or the payroll-manager grant) before any
call in here, so there is no per-row scope clause.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.payout_verification import (
    PayoutMismatch,
    PayoutRow,
    PayoutRunKind,
    PayoutUpload,
)


@dataclass(frozen=True)
class EmployeeIdentityRow:
    """The few employee columns payout matching needs - not the whole record."""

    id: uuid.UUID
    employee_number: str | None
    work_email: str


class PayoutVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def employee_identities(self) -> list[EmployeeIdentityRow]:
        """Every employee, active or not: someone offboarded mid-month can still
        be on this month's payout and must match, not show up as unknown."""
        rows = await self._session.execute(
            select(Employee.id, Employee.employee_number, Employee.work_email)
        )
        return [EmployeeIdentityRow(id=r[0], employee_number=r[1], work_email=r[2]) for r in rows]

    async def add_upload(self, upload: PayoutUpload) -> PayoutUpload:
        self._session.add(upload)
        await self._session.flush()
        return upload

    async def add_rows(self, rows: Sequence[PayoutRow]) -> None:
        self._session.add_all(rows)
        await self._session.flush()

    async def add_mismatches(self, mismatches: Sequence[PayoutMismatch]) -> None:
        self._session.add_all(mismatches)
        await self._session.flush()

    async def find_same_file(
        self, period_month: str, kind: PayoutRunKind, file_sha256: str
    ) -> PayoutUpload | None:
        """An earlier upload of these exact bytes for the same month and run."""
        record: PayoutUpload | None = await self._session.scalar(
            select(PayoutUpload)
            .where(
                PayoutUpload.period_month == period_month,
                PayoutUpload.kind == kind,
                PayoutUpload.file_sha256 == file_sha256,
            )
            .order_by(PayoutUpload.created_at.desc())
            .limit(1)
        )
        return record

    async def get_upload(self, upload_id: uuid.UUID) -> PayoutUpload | None:
        return await self._session.get(PayoutUpload, upload_id)

    async def latest_upload(self, period_month: str | None) -> PayoutUpload | None:
        query = select(PayoutUpload)
        if period_month is not None:
            query = query.where(PayoutUpload.period_month == period_month)
        record: PayoutUpload | None = await self._session.scalar(
            query.order_by(PayoutUpload.created_at.desc()).limit(1)
        )
        return record

    async def list_uploads(
        self, period_month: str | None, *, limit: int, offset: int
    ) -> tuple[Sequence[PayoutUpload], int]:
        query = select(PayoutUpload)
        if period_month is not None:
            query = query.where(PayoutUpload.period_month == period_month)
        total = await self._session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self._session.execute(
            query.order_by(PayoutUpload.created_at.desc()).offset(offset).limit(limit)
        )
        return rows.scalars().all(), int(total or 0)

    async def mismatches_for(self, upload_id: uuid.UUID) -> Sequence[PayoutMismatch]:
        rows = await self._session.execute(
            select(PayoutMismatch)
            .where(PayoutMismatch.upload_id == upload_id)
            .order_by(PayoutMismatch.kind, PayoutMismatch.created_at)
        )
        return rows.scalars().all()

    async def get_mismatch(
        self, upload_id: uuid.UUID, mismatch_id: uuid.UUID
    ) -> PayoutMismatch | None:
        record: PayoutMismatch | None = await self._session.scalar(
            select(PayoutMismatch).where(
                PayoutMismatch.id == mismatch_id, PayoutMismatch.upload_id == upload_id
            )
        )
        return record

    async def rows_by_id(self, row_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, PayoutRow]:
        if not row_ids:
            return {}
        rows = await self._session.execute(select(PayoutRow).where(PayoutRow.id.in_(row_ids)))
        return {r.id: r for r in rows.scalars().all()}

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        """Make the verification durable before anyone is told about it."""
        await self._session.commit()
