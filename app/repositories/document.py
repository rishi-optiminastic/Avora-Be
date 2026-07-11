"""Employee document data access — the only place document queries are built.

No scope clause here; the service authorizes (HR/Admin or self) before calling.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentCategory, EmployeeDocument
from app.schemas.document import DocumentCreate


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeDocument]:
        result = await self._session.scalars(
            select(EmployeeDocument)
            .where(EmployeeDocument.employee_id == employee_id)
            .order_by(EmployeeDocument.created_at.desc())
        )
        return result.all()

    async def get(self, document_id: uuid.UUID) -> EmployeeDocument | None:
        return await self._session.get(EmployeeDocument, document_id)

    async def add(
        self, employee_id: uuid.UUID, data: DocumentCreate, *, uploaded_by: uuid.UUID
    ) -> EmployeeDocument:
        document = EmployeeDocument(
            employee_id=employee_id,
            title=data.title.strip(),
            category=data.category,
            url=data.url,
            uploaded_by=uploaded_by,
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def add_file(
        self,
        employee_id: uuid.UUID,
        *,
        title: str,
        category: DocumentCategory,
        content_type: str,
        byte_size: int,
        original_filename: str | None,
        object_key: str | None,
        content: bytes | None,
        uploaded_by: uuid.UUID,
    ) -> EmployeeDocument:
        document = EmployeeDocument(
            employee_id=employee_id,
            title=title.strip(),
            category=category,
            url=None,
            content_type=content_type,
            byte_size=byte_size,
            original_filename=original_filename,
            object_key=object_key,
            content=content,
            uploaded_by=uploaded_by,
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def delete(self, document: EmployeeDocument) -> None:
        await self._session.delete(document)
        await self._session.flush()
