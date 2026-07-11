"""Workspace file business rules.

This is a *shared* workspace, not a sensitive personal record store: any signed-in
employee may browse, upload, and download files (org-wide visibility is the whole
point — it's a team drive). The one restricted action is delete, which only the
uploader or HR/Admin may do. Every action is audited (Security rule 5.7).

Bytes go to S3 when configured (only the key is stored), else to the DB `content`
column — the same fallback `ScreenshotService` uses, so local/test runs need no S3.
"""

from __future__ import annotations

import uuid

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
from app.models.employee import Role
from app.models.workspace_file import (
    WorkspaceFile,
    WorkspaceFileCategory,
    WorkspaceVisibility,
)
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_entity import WorkEntityRepository
from app.repositories.workspace_file import FileRow, WorkspaceFileRepository
from app.schemas.auth import CurrentUser
from app.schemas.workspace_file import (
    AccessSpec,
    WorkspaceFileMeta,
    WorkspaceFileRead,
    WorkspaceFileStats,
    WorkspaceLinkCreate,
)

logger = get_logger("app.workspace_files")

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB — keep the DB-fallback path sane.
MAX_LIST = 200

# MIME types a browser will execute inline. We never trust the uploader's type for
# these — store them as a neutral type so even a leaked link can't run script
# (defence-in-depth alongside the forced-attachment download).
_UNSAFE_INLINE_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/javascript",
        "text/javascript",
        "application/xml",
        "text/xml",
    }
)


def _safe_content_type(content_type: str) -> str:
    media = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    if not media or media in _UNSAFE_INLINE_TYPES:
        return "application/octet-stream"
    return media


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


def _to_read(row: FileRow) -> WorkspaceFileRead:
    file, project_name, uploader_name = row
    return WorkspaceFileRead(
        id=file.id,
        name=file.name,
        description=file.description,
        category=file.category,
        content_type=file.content_type,
        byte_size=file.byte_size,
        original_filename=file.original_filename,
        url=file.url,
        visibility=file.visibility,
        visible_departments=list(file.visible_departments or []),
        visible_employee_ids=[uuid.UUID(i) for i in (file.visible_employee_ids or [])],
        project_id=file.project_id,
        project_name=project_name,
        uploaded_by=file.uploaded_by,
        uploader_name=uploader_name,
        created_at=file.created_at,
    )


class WorkspaceFileService:
    def __init__(
        self,
        files: WorkspaceFileRepository,
        entities: WorkEntityRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._files = files
        self._entities = entities
        self._employees = employees
        self._audit = audit
        self._settings = settings

    async def _caller_department(self, caller: CurrentUser) -> str | None:
        employee = await self._employees.get(caller.employee_id)
        return employee.department if employee is not None else None

    def _can_view(self, caller: CurrentUser, file: WorkspaceFile, department: str | None) -> bool:
        """May THIS caller see this entry? Admin/HR and the uploader always can;
        an EVERYONE entry is open; a RESTRICTED one needs a department or
        individual match."""
        if _can_manage(caller) or file.uploaded_by == caller.employee_id:
            return True
        if file.visibility is WorkspaceVisibility.EVERYONE:
            return True
        if department and department in (file.visible_departments or []):
            return True
        return str(caller.employee_id) in (file.visible_employee_ids or [])

    @staticmethod
    def _normalize_access(access: AccessSpec) -> tuple[WorkspaceVisibility, list[str], list[str]]:
        """An EVERYONE entry carries no ACL; a RESTRICTED one keeps the (de-duped)
        departments + employee ids. Employee ids are stored as strings (JSON ACL)."""
        if access.visibility is WorkspaceVisibility.EVERYONE:
            return WorkspaceVisibility.EVERYONE, [], []
        departments = list(
            dict.fromkeys(d.strip() for d in access.visible_departments if d.strip())
        )
        employee_ids = list(dict.fromkeys(str(i) for i in access.visible_employee_ids))
        return WorkspaceVisibility.RESTRICTED, departments, employee_ids

    async def list(
        self,
        caller: CurrentUser,
        *,
        project_id: uuid.UUID | None,
        category: WorkspaceFileCategory | None,
        search: str | None,
        limit: int,
    ) -> list[WorkspaceFileRead]:
        rows = await self._files.list(
            project_id=project_id,
            category=category,
            search=search,
            limit=min(max(1, limit), MAX_LIST),
        )
        department = await self._caller_department(caller)
        return [_to_read(r) for r in rows if self._can_view(caller, r[0], department)]

    async def stats(self, caller: CurrentUser) -> WorkspaceFileStats:
        s = await self._files.stats()
        return WorkspaceFileStats(
            total_files=s.total_files,
            total_bytes=s.total_bytes,
            project_count=s.project_count,
            by_category=s.by_category,
        )

    async def upload(
        self,
        caller: CurrentUser,
        meta: WorkspaceFileMeta,
        data: bytes,
        *,
        filename: str | None,
        content_type: str,
    ) -> WorkspaceFileRead:
        if not data:
            raise ValidationError("File is empty.")
        if len(data) > MAX_FILE_BYTES:
            raise ValidationError("File is too large (max 25 MB).")
        if meta.project_id is not None and await self._entities.get(meta.project_id) is None:
            raise NotFoundError("Project not found.")

        media_type = _safe_content_type(content_type)
        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.workspace_object_key(uuid.uuid4().hex, filename)
            try:
                await storage.put_object(object_key, data, media_type)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("workspace_file_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        visibility, departments, employee_ids = self._normalize_access(meta)
        file = await self._files.add(
            name=meta.name.strip(),
            description=(meta.description or None),
            category=meta.category,
            content_type=media_type,
            byte_size=len(data),
            original_filename=filename,
            url=None,
            visibility=visibility,
            visible_departments=departments,
            visible_employee_ids=employee_ids,
            project_id=meta.project_id,
            uploaded_by=caller.employee_id,
            object_key=object_key,
            content=stored,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace_file.upload",
            target=f"file:{file.id}",
        )
        row = await self._files.get_named(file.id)
        if row is None:  # pragma: no cover — just flushed it
            raise NotFoundError()
        return _to_read(row)

    async def create_link(
        self, caller: CurrentUser, payload: WorkspaceLinkCreate
    ) -> WorkspaceFileRead:
        """Store a link (a Google Sheet/Doc, a receipt URL, anything) — no bytes."""
        if payload.project_id is not None and await self._entities.get(payload.project_id) is None:
            raise NotFoundError("Project not found.")
        visibility, departments, employee_ids = self._normalize_access(payload)
        file = await self._files.add(
            name=payload.name.strip(),
            description=(payload.description or None),
            category=payload.category,
            content_type="text/uri-list",
            byte_size=0,
            original_filename=None,
            url=payload.url,
            visibility=visibility,
            visible_departments=departments,
            visible_employee_ids=employee_ids,
            project_id=payload.project_id,
            uploaded_by=caller.employee_id,
            object_key=None,
            content=None,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace_file.link.create",
            target=f"file:{file.id}",
        )
        row = await self._files.get_named(file.id)
        if row is None:  # pragma: no cover — just flushed it
            raise NotFoundError()
        return _to_read(row)

    async def get_for_download(self, caller: CurrentUser, file_id: uuid.UUID) -> WorkspaceFile:
        """The file *with* bytes, for the download endpoint. Restricted files are
        404 for anyone outside their access (never leak that they exist)."""
        file = await self._files.get(file_id)
        if file is None:
            raise NotFoundError()
        if not self._can_view(caller, file, await self._caller_department(caller)):
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace_file.download",
            target=f"file:{file.id}",
        )
        return file

    async def delete(self, caller: CurrentUser, file_id: uuid.UUID) -> None:
        file = await self._files.get(file_id)
        if file is None:
            raise NotFoundError()
        # Only the uploader or HR/Admin may delete (everyone else can read it).
        if not _can_manage(caller) and file.uploaded_by != caller.employee_id:
            raise AuthorizationError()
        if file.object_key and self._settings.s3_enabled:
            try:
                await storage.delete_objects([file.object_key])
            except (ClientError, BotoCoreError) as exc:
                logger.warning("workspace_file_s3_delete_failed", extra={"key": file.object_key})
                raise StorageError() from exc
        await self._files.delete(file)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="workspace_file.delete",
            target=f"file:{file_id}",
        )
