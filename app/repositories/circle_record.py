"""Data access for what Avora mirrors from Circle (personal details, appraisal
history, assets) - the only place those queries are built.

No row-scope clause lives here: the records are fetched one employee at a time
and `CircleRecordService` authorizes the caller before calling in. Writes come
only from the Circle sync.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TypeVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.circle_record import EmployeeAppraisal, EmployeeAsset, EmployeeCircleProfile

# The most rows one person's history list returns; Circle keeps a handful each.
MAX_RECORDS_PER_PERSON = 200

# Columns the sync may write on a profile (guards setattr).
PROFILE_FIELDS = frozenset(
    {
        "phone_encrypted",
        "personal_email_encrypted",
        "address_encrypted",
        "emergency_contact_encrypted",
        "employment_type",
        "pan_masked",
        "aadhaar_masked",
        "uan_masked",
    }
)
APPRAISAL_FIELDS = (
    "period",
    "status",
    "current_salary",
    "revised_salary",
    "effective_date",
    "score",
)
ASSET_FIELDS = ("name", "asset_type", "serial_number", "status", "assigned_date")

RecordValues = Mapping[str, str | float | date | None]
_Mirrored = TypeVar("_Mirrored", EmployeeAppraisal, EmployeeAsset)


class CircleRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_profile(self, employee_id: uuid.UUID) -> EmployeeCircleProfile | None:
        record: EmployeeCircleProfile | None = await self._session.scalar(
            select(EmployeeCircleProfile).where(EmployeeCircleProfile.employee_id == employee_id)
        )
        return record

    async def save_profile(
        self, employee_id: uuid.UUID, fields: Mapping[str, str | None], *, synced_at: datetime
    ) -> EmployeeCircleProfile:
        """Get-or-create, then set only the given (whitelisted) columns."""
        record = await self.get_profile(employee_id)
        if record is None:
            record = EmployeeCircleProfile(employee_id=employee_id)
            self._session.add(record)
        for key, value in fields.items():
            if key in PROFILE_FIELDS:
                setattr(record, key, value)
        record.synced_at = synced_at
        await self._session.flush()
        return record

    async def list_appraisals(self, employee_id: uuid.UUID) -> Sequence[EmployeeAppraisal]:
        rows = await self._session.scalars(
            select(EmployeeAppraisal)
            .where(EmployeeAppraisal.employee_id == employee_id)
            .order_by(
                EmployeeAppraisal.effective_date.desc().nulls_last(),
                EmployeeAppraisal.circle_id,
            )
            .limit(MAX_RECORDS_PER_PERSON)
        )
        return rows.all()

    async def list_assets(self, employee_id: uuid.UUID) -> Sequence[EmployeeAsset]:
        rows = await self._session.scalars(
            select(EmployeeAsset)
            .where(EmployeeAsset.employee_id == employee_id)
            .order_by(EmployeeAsset.circle_id)
            .limit(MAX_RECORDS_PER_PERSON)
        )
        return rows.all()

    async def replace_appraisals(
        self, employee_id: uuid.UUID, entries: Mapping[str, RecordValues]
    ) -> bool:
        """Make the person's appraisals exactly `entries` (keyed by Circle id).
        Returns whether anything changed."""
        return await self._replace(EmployeeAppraisal, employee_id, entries, APPRAISAL_FIELDS)

    async def replace_assets(
        self, employee_id: uuid.UUID, entries: Mapping[str, RecordValues]
    ) -> bool:
        return await self._replace(EmployeeAsset, employee_id, entries, ASSET_FIELDS)

    async def _replace(
        self,
        model: type[_Mirrored],
        employee_id: uuid.UUID,
        entries: Mapping[str, RecordValues],
        fields: Sequence[str],
    ) -> bool:
        # Every row (no list cap), so nothing is mistaken for new and duplicated.
        rows = await self._session.scalars(select(model).where(model.employee_id == employee_id))
        current: dict[str, _Mirrored] = {row.circle_id: row for row in rows.all()}
        changed = False
        gone = set(current) - set(entries)
        if gone:
            await self._session.execute(
                delete(model).where(model.employee_id == employee_id, model.circle_id.in_(gone))
            )
            changed = True
        for circle_id, values in entries.items():
            row = current.get(circle_id)
            if row is None:
                row = model(employee_id=employee_id, circle_id=circle_id)
                self._session.add(row)
                changed = True
            for key in fields:
                if getattr(row, key, None) != values.get(key):
                    setattr(row, key, values.get(key))
                    changed = True
        await self._session.flush()
        return changed
