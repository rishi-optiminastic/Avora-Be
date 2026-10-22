"""Compensation data access — the only place compensation queries are built.

No row-scope clause lives here: compensation is fetched one employee at a time
and the *service* authorizes the caller (HR/Admin or self) before calling in.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compensation import AccountType, Compensation, PayPeriod
from app.schemas.compensation import CompensationWrite

# The columns Circle owns (plus the effective date it may fill). The Circle
# sync may set these and nothing else; HR may not edit them in Avora once the
# person is in Circle (see CompensationService).
CIRCLE_OWNED_FIELDS = frozenset(
    {
        "amount_minor",
        "currency",
        "period",
        "pf_enabled",
        "bank_name",
        "account_number_encrypted",
        "ifsc_code",
        "effective_date",
    }
)

CircleValue = int | bool | str | PayPeriod | date | None


class CompensationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_employee(self, employee_id: uuid.UUID) -> Compensation | None:
        record: Compensation | None = await self._session.scalar(
            select(Compensation).where(Compensation.employee_id == employee_id)
        )
        return record

    async def get_for_employees(
        self, employee_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Compensation]:
        """Batch lookup keyed by employee_id — avoids the per-employee N+1 in the
        org-wide payroll estimate."""
        if not employee_ids:
            return {}
        rows = await self._session.execute(
            select(Compensation).where(Compensation.employee_id.in_(employee_ids))
        )
        return {c.employee_id: c for c in rows.scalars().all()}

    async def upsert(
        self, employee_id: uuid.UUID, data: CompensationWrite, *, updated_by: uuid.UUID
    ) -> Compensation:
        record = await self.get_for_employee(employee_id)
        if record is None:
            record = Compensation(employee_id=employee_id)
            self._session.add(record)
        record.amount_minor = data.amount_minor
        record.bonus_minor = data.bonus_minor
        record.currency = data.currency.upper()
        record.period = data.period
        record.effective_date = data.effective_date
        record.pf_enabled = data.pf_enabled
        record.note = data.note
        record.updated_by = updated_by
        await self._session.flush()
        return record

    async def upsert_bank(
        self,
        employee_id: uuid.UUID,
        *,
        account_holder_name: str | None,
        bank_name: str | None,
        account_number_encrypted: str | None,
        ifsc_code: str | None,
        account_type: AccountType | None,
        updated_by: uuid.UUID,
    ) -> Compensation:
        """Set only the bank fields (get-or-create), leaving pay untouched. The
        account number arrives already encrypted; the repo never sees plaintext."""
        record = await self.get_for_employee(employee_id)
        if record is None:
            record = Compensation(employee_id=employee_id)
            self._session.add(record)
        record.account_holder_name = account_holder_name
        record.bank_name = bank_name
        record.account_number_encrypted = account_number_encrypted
        record.ifsc_code = ifsc_code
        record.account_type = account_type
        record.updated_by = updated_by
        await self._session.flush()
        return record

    async def apply_circle(
        self, employee_id: uuid.UUID, fields: Mapping[str, CircleValue]
    ) -> Compensation:
        """Set Circle's values (get-or-create), touching only the columns Circle
        owns. `updated_by` is cleared: the change was the sync's, not a person's
        (the audit log names `circle-sync`)."""
        record = await self.get_for_employee(employee_id)
        if record is None:
            record = Compensation(employee_id=employee_id)
            self._session.add(record)
        for key, value in fields.items():
            if key in CIRCLE_OWNED_FIELDS:
                setattr(record, key, value)
        record.updated_by = None
        await self._session.flush()
        return record
