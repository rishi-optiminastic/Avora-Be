"""Data access for the Circle document copy ledger."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.circle_document_import import CircleDocumentImport


class CircleDocumentImportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def copied_ids(self, employee_id: uuid.UUID) -> set[str]:
        """Circle document ids already handled for this person: copied (even if
        the copy was later deleted in Avora) or skipped as uncopyable."""
        rows = await self._session.scalars(
            select(CircleDocumentImport.circle_document_id).where(
                CircleDocumentImport.employee_id == employee_id
            )
        )
        return set(rows.all())

    async def record(
        self, employee_id: uuid.UUID, circle_document_id: str, document_id: uuid.UUID | None
    ) -> None:
        """`document_id` None = looked at but not copied (e.g. too large)."""
        self._session.add(
            CircleDocumentImport(
                employee_id=employee_id,
                circle_document_id=circle_document_id,
                document_id=document_id,
            )
        )
        await self._session.flush()
