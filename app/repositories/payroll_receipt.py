"""Payroll-receipt data access - the proof-of-payment documents for a month."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll_receipt import PayrollReceipt


class PayrollReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, receipt_id: uuid.UUID) -> PayrollReceipt | None:
        return await self._session.get(PayrollReceipt, receipt_id)

    async def list_for_month(self, period_month: str) -> Sequence[PayrollReceipt]:
        """Newest first - the most recent evidence is what someone asking about
        the month usually wants."""
        rows = await self._session.scalars(
            select(PayrollReceipt)
            .where(PayrollReceipt.period_month == period_month)
            .order_by(PayrollReceipt.created_at.desc())
        )
        return list(rows.all())

    async def find_duplicate(self, period_month: str, file_sha256: str) -> PayrollReceipt | None:
        """The same bytes already filed against the same month.

        Re-uploading the same advice is an accident, not a second payment, and
        silently keeping both would overstate what was paid.
        """
        found: PayrollReceipt | None = await self._session.scalar(
            select(PayrollReceipt).where(
                PayrollReceipt.period_month == period_month,
                PayrollReceipt.file_sha256 == file_sha256,
            )
        )
        return found

    async def counts_by_month(self, months: Sequence[str]) -> dict[str, int]:
        """How many receipts each month has. One query, so a list of runs can be
        badged without a lookup per row."""
        if not months:
            return {}
        rows = await self._session.execute(
            select(PayrollReceipt.period_month, func.count())
            .where(PayrollReceipt.period_month.in_(months))
            .group_by(PayrollReceipt.period_month)
        )
        return dict(rows.all())  # type: ignore[arg-type]

    def add(self, receipt: PayrollReceipt) -> None:
        self._session.add(receipt)

    async def delete(self, receipt: PayrollReceipt) -> None:
        await self._session.delete(receipt)

    async def flush(self) -> None:
        await self._session.flush()
