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
from app.models.workspace_file import WorkspaceFile, WorkspaceFileCategory
from app.repositories.audit import AuditRepository
from app.repositories.work_entity import WorkEntityRepository
from app.repositories.workspace_file import FileRow, WorkspaceFileRepository
from app.schemas.auth import CurrentUser
from app.schemas.workspace_file import WorkspaceFileMeta, WorkspaceFileRead, WorkspaceFileStats

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
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._files = files
        self._entities = entities
        self._audit = audit
        self._settings = settings

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
        return [_to_read(r) for r in rows]

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

        file = await self._files.add(
            name=meta.name.strip(),
            description=(meta.description or None),
            category=meta.category,
            content_type=media_type,
            byte_size=len(data),
            original_filename=filename,
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

    async def get_for_download(self, caller: CurrentUser, file_id: uuid.UUID) -> WorkspaceFile:
        """The file *with* bytes, for the download endpoint (org-wide read)."""
        file = await self._files.get(file_id)
        if file is None:
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
