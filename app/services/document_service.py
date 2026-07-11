"""Employee document business rules — same visibility tier as compensation.

Read: HR/Admin or the person themselves. Write/delete: HR/Admin only. Every
action is audited (Security rule 5.7).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.document import EmployeeDocument
from app.models.employee import Role
from app.repositories.audit import AuditRepository
from app.repositories.document import DocumentRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.document import DocumentCreate, DocumentFileMeta

logger = get_logger("app.document")

MAX_DOC_BYTES = 25 * 1024 * 1024  # 25 MB — keep the DB-fallback path sane.


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class DocumentService:
    def __init__(
        self,
        documents: DocumentRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._documents = documents
        self._employees = employees
        self._audit = audit
        self._settings = settings

    def _assert_can_view(self, caller: CurrentUser, employee_id: uuid.UUID) -> None:
        if not _can_manage(caller) and caller.employee_id != employee_id:
            raise AuthorizationError()

    async def list(self, caller: CurrentUser, employee_id: uuid.UUID) -> Sequence[EmployeeDocument]:
        self._assert_can_view(caller, employee_id)
        documents = await self._documents.list_for_employee(employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.list",
            target=f"employee:{employee_id}",
        )
        return documents

    async def add(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: DocumentCreate
    ) -> EmployeeDocument:
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        document = await self._documents.add(employee_id, data, uploaded_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.add",
            target=f"employee:{employee_id}",
        )
        return document

    async def add_file(
        self,
        caller: CurrentUser,
        employee_id: uuid.UUID,
        meta: DocumentFileMeta,
        data: bytes,
        *,
        filename: str | None,
        content_type: str,
    ) -> EmployeeDocument:
        """Upload an actual document file (HR/Admin only). Bytes go to S3 when
        configured (only the key is stored), else to the in-DB `content` column."""
        if not _can_manage(caller):
            raise AuthorizationError()
        if not data:
            raise ValidationError("File is empty.")
        if len(data) > MAX_DOC_BYTES:
            raise ValidationError("File is too large (max 25 MB).")
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()

        media_type = (content_type or "application/octet-stream").split(";")[0].strip().lower()
        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.workspace_object_key(uuid.uuid4().hex, filename)
            try:
                await storage.put_object(object_key, data, media_type)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("document_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        document = await self._documents.add_file(
            employee_id,
            title=meta.title,
            category=meta.category,
            content_type=media_type,
            byte_size=len(data),
            original_filename=filename,
            object_key=object_key,
            content=stored,
            uploaded_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.upload",
            target=f"employee:{employee_id}:document:{document.id}",
        )
        return document

    async def get_for_download(
        self, caller: CurrentUser, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> EmployeeDocument:
        """An uploaded document's bytes, for the download endpoint. HR/Admin or the
        person themselves may read; anything else is 404 (never leaks existence)."""
        document = await self._documents.get(document_id)
        if document is None or document.employee_id != employee_id or document.url is not None:
            raise NotFoundError()
        if not _can_manage(caller) and caller.employee_id != employee_id:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.download",
            target=f"employee:{employee_id}:document:{document_id}",
        )
        return document

    async def delete(
        self, caller: CurrentUser, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        document = await self._documents.get(document_id)
        if document is None or document.employee_id != employee_id:
            raise NotFoundError()
        if document.object_key and self._settings.s3_enabled:
            try:
                await storage.delete_objects([document.object_key])
            except (ClientError, BotoCoreError):
                logger.warning("document_s3_delete_failed", extra={"key": document.object_key})
        await self._documents.delete(document)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.delete",
            target=f"employee:{employee_id}",
        )
